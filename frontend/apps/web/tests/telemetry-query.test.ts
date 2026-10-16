import assert from "node:assert/strict"
import { afterEach, test } from "bun:test"
import * as Sentry from "@sentry/react"
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
import { createTelemetryOptions, reportFailure } from "../src/lib/telemetry"
import { parseTelemetryConfig } from "../src/lib/telemetry-policy"

type Envelope = Parameters<
  ReturnType<NonNullable<Sentry.BrowserOptions["transport"]>>["send"]
>[0]
const cleanups: (() => void)[] = []
const secret = "private-query-draft-session-model-token"

afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup()
  await Sentry.close(1000)
  Sentry.setCurrentClient(undefined)
})

function setup() {
  const envelopes: Envelope[] = []
  const telemetry = Sentry.init({
    ...createTelemetryOptions(
      parseTelemetryConfig({
        VITE_SENTRY_ENABLED: "true",
        VITE_SENTRY_DSN: "https://public@o123.ingest.sentry.io/456",
      })
    ),
    transport: () => ({
      send(envelope) {
        envelopes.push(envelope)
        return Promise.resolve({ statusCode: 200 })
      },
      flush: () => Promise.resolve(true),
    }),
  })!
  const client = createSessionQueryClient()
  cleanups.push(() => disposeSessionQueryClient(client))
  const events = async () => {
    await telemetry.flush(1000)
    assert.equal(JSON.stringify(envelopes).includes(secret), false)
    return envelopes.flatMap((envelope) =>
      envelope[1].flatMap(([header, payload]) =>
        header.type === "event" ? [payload as Sentry.ErrorEvent] : []
      )
    )
  }
  return { client, events }
}

test("a successful retry emits no issue and preserves the existing retry policy", async () => {
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
  assert.deepEqual(await events(), [])
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
      reportFailure(error, "workspace.bootstrap")
      return true
    }
  )
  assert.equal(attempts, 2)
  const captured = await events()
  assert.equal(captured.length, 1)
  assert.equal(captured[0]?.tags?.["telemetry.operation"], "settings.load")
})

test("two observers sharing a failed query emit one terminal issue", async () => {
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
  assert.equal((await events()).length, 1)
})

test("draft query keys and mutation variables never enter SDK envelopes", async () => {
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
    meta: { telemetryOperation: "settings.save", telemetryOwner: "cache" },
    mutationFn: async (variables: { sessionId: string; modelId: string }) => {
      assert.equal(variables.modelId, secret)
      throw new ApiError("service_unavailable", secret, 503)
    },
  })
  await assert.rejects(mutation.mutate({ sessionId: secret, modelId: secret }))
  const captured = await events()
  assert.equal(captured.length, 2)
  assert.deepEqual(
    captured.map((event) => event.tags?.["telemetry.operation"]),
    ["context.preview", "settings.save"]
  )
})

test("cancellation and client disposal suppress pending queries and mutations", async () => {
  const { client, events } = setup()
  const query = client
    .fetchQuery({
      queryKey: ["cancelled", secret],
      meta: { telemetryOperation: "history.load" },
      queryFn: () => new Promise<never>(() => undefined),
    })
    .catch((error: unknown) => error)
  await client.cancelQueries({}, { silent: true })
  await query

  let rejectQuery!: (error: Error) => void
  const disposedQuery = client
    .fetchQuery({
      queryKey: ["disposed", secret],
      meta: { telemetryOperation: "history.load" },
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
    meta: { telemetryOperation: "settings.save", telemetryOwner: "cache" },
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
  assert.deepEqual(await events(), [])
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
  assert.deepEqual(await events(), [])
  recovering = true
  await client.fetchInfiniteQuery({ ...options, staleTime: 0 })
  assert.deepEqual(await events(), [])
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
        reportFailure(error, "activity.summaries")
    })
  currentSelection = 2
  rejectRead(new ApiError("service_unavailable", secret, 503))
  await pending
  assert.deepEqual(await events(), [])

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
  const captured = await events()
  assert.equal(captured.length, 1)
  assert.equal(captured[0]?.tags?.["telemetry.operation"], "activity.summaries")
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
      reportFailure(error, "sessions.open")
      reportFailure(error, "sessions.open")
      return true
    }
  )
  const captured = await events()
  assert.equal(captured.length, 1)
  assert.equal(captured[0]?.tags?.["telemetry.operation"], "sessions.open")
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
      if (currentSession === originalSession)
        reportFailure(error, "settings.save")
    })
  await started
  currentSession = "new-session"
  rejectSave(new ApiError("service_unavailable", secret, 503))
  await pending
  assert.deepEqual(await events(), [])

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
      reportFailure(error, "settings.save")
      reportFailure(error, "settings.save")
      return true
    }
  )
  assert.equal((await events()).length, 1)
})
