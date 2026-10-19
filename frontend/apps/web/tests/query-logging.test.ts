import assert from "node:assert/strict"
import { afterEach, test, mock, spyOn } from "bun:test"
import {
  InfiniteQueryObserver,
  MutationObserver,
  QueryObserver,
} from "@tanstack/react-query"

import { ApiError } from "../src/lib/api"
import {
  createSessionQueryClient,
  disposeSessionQueryClient,
} from "../src/lib/query-client"
import {
  contextPreviewQueryOptions,
  sessionModelMutationOptions,
  settingsQueryOptions,
  sessionHistoryQueryOptions,
  runSummariesQueryOptions,
} from "../src/lib/server-queries"
import { logFailure } from "../src/lib/logging"
const cleanups: (() => void)[] = []
const secret = "private-query-draft-session-model-token"
const records: Record<string, unknown>[] = []

afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup()
  records.length = 0
  mock.restore()
})

function setup() {
  spyOn(console, "error").mockImplementation((record: unknown) => {
    records.push(record as Record<string, unknown>)
  })
  spyOn(console, "warn").mockImplementation((record: unknown) => {
    records.push(record as Record<string, unknown>)
  })
  const client = createSessionQueryClient()
  cleanups.push(() => disposeSessionQueryClient(client))
  const events = () => {
    assert.equal(JSON.stringify(records).includes(secret), false)
    return records
  }
  return { client, events }
}

test("a successful retry emits no log and preserves the existing retry policy", async () => {
  const { client, events } = setup()
  let attempts = 0
  const settings = { selected_provider: "openai", providers: [] }
  const result = await client.fetchQuery({
    ...settingsQueryOptions({
      getSettings: async () => {
        if (++attempts === 1)
          throw new ApiError("service_unavailable", secret, 503)
        return settings
      },
    }),
    retryDelay: 0,
  })
  assert.equal(attempts, 2)
  assert.equal(result, settings)
  assert.deepEqual(events(), [])
})

test("exhausted retries emit once even when the final error reaches an outer handler", async () => {
  const { client, events } = setup()
  let attempts = 0
  const failure = new ApiError("service_unavailable", secret, 503)
  await assert.rejects(
    client.fetchQuery({
      ...settingsQueryOptions({
        getSettings: async () => {
          attempts++
          throw failure
        },
      }),
      retryDelay: 0,
    }),
    (error) => {
      assert.equal(error, failure)
      logFailure(error, "workspace.bootstrap")
      return true
    }
  )
  assert.equal(attempts, 2)
  const captured = events()
  assert.equal(captured.length, 1)
  assert.deepEqual(captured[0], {
    timestamp: captured[0]?.timestamp,
    level: "error",
    event: "operation.failed",
    operation: "settings.load",
    failure: "service_unavailable",
    status: 503,
  })
})

test("two observers sharing a failed query emit one terminal log", async () => {
  const { client, events } = setup()
  const options = {
    ...settingsQueryOptions({
      getSettings: async () => {
        throw new ApiError("service_unavailable", secret, 503)
      },
    }),
    retry: false,
  }
  const first = new QueryObserver(client, options)
  const second = new QueryObserver(client, options)
  let failed!: () => void
  const terminal = new Promise<void>((resolve) => {
    failed = resolve
  })
  cleanups.push(
    first.subscribe((result) => {
      if (result.isError) failed()
    })
  )
  cleanups.push(second.subscribe(() => undefined))
  await terminal
  assert.equal(events().length, 1)
})

test("draft query keys and mutation variables never enter browser logs", async () => {
  const { client, events } = setup()
  await assert.rejects(
    client.fetchQuery(
      contextPreviewQueryOptions(
        {
          session_id: secret,
          model_id: secret,
          draft: secret,
          budget_preset: "conservative",
        },
        1,
        {
          previewContext: async () => {
            throw new ApiError("service_unavailable", secret, 503)
          },
        }
      )
    )
  )
  const mutation = new MutationObserver(client, {
    meta: { logOperation: "settings.save", logOwner: "cache" },
    mutationFn: async (variables: { sessionId: string; modelId: string }) => {
      assert.equal(variables.modelId, secret)
      throw new ApiError("service_unavailable", secret, 503)
    },
  })
  await assert.rejects(mutation.mutate({ sessionId: secret, modelId: secret }))
  const captured = events()
  assert.equal(captured.length, 2)
  assert.deepEqual(
    captured.map((event) => event.operation),
    ["context.preview", "settings.save"]
  )
})

test("cancellation and client disposal suppress pending queries and mutations", async () => {
  const { client, events } = setup()
  const query = client
    .fetchQuery({
      queryKey: ["cancelled", secret],
      meta: { logOperation: "history.load" },
      queryFn: () => new Promise<never>(() => undefined),
    })
    .catch((error: unknown) => error)
  await client.cancelQueries({}, { silent: true })
  await query

  let rejectQuery!: (error: Error) => void
  const disposedQuery = client
    .fetchQuery({
      queryKey: ["disposed", secret],
      meta: { logOperation: "history.load" },
      queryFn: () =>
        new Promise<never>((_resolve, reject) => {
          rejectQuery = reject
        }),
      retry: false,
    })
    .catch((error: unknown) => error)

  let rejectMutation!: (error: Error) => void
  let started!: () => void
  const mutationStarted = new Promise<void>((resolve) => {
    started = resolve
  })
  const mutation = new MutationObserver(client, {
    meta: { logOperation: "settings.save", logOwner: "cache" },
    mutationFn: (variables: { sessionId: string; modelId: string }) => {
      assert.equal(variables.modelId, secret)
      return new Promise<never>((_resolve, reject) => {
        rejectMutation = reject
        started()
      })
    },
  })
  const pending = mutation
    .mutate({ sessionId: secret, modelId: secret })
    .catch((error: unknown) => error)
  await mutationStarted
  disposeSessionQueryClient(client)
  rejectQuery(new ApiError("service_unavailable", secret, 503))
  rejectMutation(new ApiError("service_unavailable", secret, 503))
  await disposedQuery
  await pending
  assert.deepEqual(events(), [])
})

test("history recovery stays quiet after exhausted retries followed by success with an observer", async () => {
  const { client, events } = setup()
  let recovering = false
  let attempts = 0
  const detail = {
    session: {
      id: secret,
      title: "",
      created_at: "",
      updated_at: "",
      message_count: 0,
    },
    messages: [],
    timeline_order: [],
    next_before_cursor: null,
  }
  const service = {
    getSessionHistory: async () => {
      attempts++
      if (!recovering) throw new ApiError("service_unavailable", secret, 503)
      return detail
    },
  }
  const options = {
    ...sessionHistoryQueryOptions(secret, service),
    retryDelay: 0,
  }
  const observer = new InfiniteQueryObserver(client, {
    ...options,
    enabled: false,
  })
  cleanups.push(observer.subscribe(() => undefined))
  const pending = client.fetchInfiniteQuery(options)
  observer.setOptions({
    ...sessionHistoryQueryOptions(secret, service),
    retryDelay: 0,
    enabled: false,
  })
  await assert.rejects(pending)
  assert.equal(attempts, 2)
  assert.deepEqual(events(), [])
  recovering = true
  await client.fetchInfiniteQuery({ ...options, staleTime: 0 })
  assert.deepEqual(events(), [])
})

test("stale session summary reads stay quiet while visible turn activity stays cache-owned", async () => {
  const { client, events } = setup()
  let rejectRead!: (error: Error) => void
  let currentSelection = 1
  const selection = currentSelection
  const pending = client
    .fetchInfiniteQuery({
      ...runSummariesQueryOptions(secret, {
        listRunSummaries: () =>
          new Promise<never>((_resolve, reject) => {
            rejectRead = reject
          }),
      }),
      retry: false,
    })
    .catch((error: unknown) => {
      if (selection === currentSelection)
        logFailure(error, "activity.summaries")
    })
  currentSelection = 2
  rejectRead(new ApiError("service_unavailable", secret, 503))
  await pending
  assert.deepEqual(events(), [])

  await assert.rejects(
    client.fetchInfiniteQuery({
      ...runSummariesQueryOptions(
        secret,
        {
          listRunSummaries: async () => {
            throw new ApiError("service_unavailable", secret, 503)
          },
        },
        "visible-turn"
      ),
      retry: false,
    })
  )
  const captured = events()
  assert.equal(captured.length, 1)
  assert.equal(captured[0]?.operation, "activity.summaries")
})

test("caller-owned final history failures report once under their final operation", async () => {
  const { client, events } = setup()
  const failure = new ApiError("service_unavailable", secret, 503)
  await assert.rejects(
    client.fetchInfiniteQuery({
      ...sessionHistoryQueryOptions(secret, {
        getSessionHistory: async () => {
          throw failure
        },
      }),
      retry: false,
    }),
    (error) => {
      logFailure(error, "sessions.open")
      logFailure(error, "sessions.open")
      return true
    }
  )
  const captured = events()
  assert.equal(captured.length, 1)
  assert.equal(captured[0]?.operation, "sessions.open")
})

test("session model saves report only after the caller confirms current ownership", async () => {
  const { client, events } = setup()
  let rejectSave!: (error: Error) => void
  let saveStarted!: () => void
  const started = new Promise<void>((resolve) => {
    saveStarted = resolve
  })
  let currentSession = secret
  const originalSession = currentSession
  const stale = new MutationObserver(
    client,
    sessionModelMutationOptions(client, {
      selectSessionModel: () =>
        new Promise<never>((_resolve, reject) => {
          rejectSave = reject
          saveStarted()
        }),
    })
  )
  const pending = stale
    .mutate({ sessionId: secret, modelId: secret })
    .catch((error: unknown) => {
      if (currentSession === originalSession) logFailure(error, "settings.save")
    })
  await started
  currentSession = "new-session"
  rejectSave(new ApiError("service_unavailable", secret, 503))
  await pending
  assert.deepEqual(events(), [])

  const current = new MutationObserver(
    client,
    sessionModelMutationOptions(client, {
      selectSessionModel: async () => {
        throw new ApiError("service_unavailable", secret, 503)
      },
    })
  )
  await assert.rejects(
    current.mutate({ sessionId: currentSession, modelId: secret }),
    (error) => {
      logFailure(error, "settings.save")
      logFailure(error, "settings.save")
      return true
    }
  )
  assert.equal(events().length, 1)
})
