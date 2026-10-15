import { expect, test } from "bun:test"
import {
  createSessionQueryClient,
  disposeSessionQueryClient,
} from "../apps/web/src/lib/query-client.ts"

async function helpers() {
  return import("../apps/web/src/lib/server-queries.ts")
}

test("settings and catalog reads share one cancellable query instead of duplicate HTTP calls", async () => {
  const { settingsQueryOptions } = await helpers()
  const client = createSessionQueryClient()
  let calls = 0
  let signal
  const service = {
    getSettings: async (options) => {
      calls += 1
      signal = options.signal
      return { selected_model_id: "default", models: [{ id: "ready" }] }
    },
  }
  const [settings, catalog] = await Promise.all([
    client.fetchQuery(settingsQueryOptions(service)),
    client.fetchQuery(settingsQueryOptions(service)),
  ])
  expect(settings).toBe(catalog)
  expect(calls).toBe(1)
  expect(signal).toBeInstanceOf(AbortSignal)
  disposeSessionQueryClient(client)
})

test("a model save cancels stale chat reads and updates that chat without changing the default", async () => {
  const { sessionModelMutationOptions, serverKeys } = await helpers()
  const client = createSessionQueryClient()
  const session = { id: "chat-a", model_id: "old" }
  const detail = {
    session,
    messages: [{ id: "saved-answer" }],
    assistant_outputs: [{ id: "partial" }],
  }
  client.setQueryData(serverKeys.session("chat-a"), detail)
  client.setQueryData(serverKeys.session("chat-b"), {
    session: { id: "chat-b", model_id: "other" },
  })
  client.setQueryData(serverKeys.sessions, [session])
  client.setQueryData(serverKeys.settings, { selected_model_id: "default" })
  let signal
  const staleRead = client
    .fetchQuery({
      queryKey: serverKeys.session("chat-a"),
      staleTime: 0,
      queryFn: ({ signal: requestSignal }) => {
        signal = requestSignal
        return new Promise(() => {})
      },
    })
    .catch((error) => error)
  const calls = []
  const mutation = client.getMutationCache().build(
    client,
    sessionModelMutationOptions(client, {
      selectSessionModel: async (sessionId, modelId) => {
        calls.push({ sessionId, modelId })
        return { ...session, model_id: modelId }
      },
    })
  )
  await mutation.execute({ sessionId: "chat-a", modelId: "new" })
  await staleRead
  expect(signal.aborted).toBe(true)
  expect(calls).toEqual([{ sessionId: "chat-a", modelId: "new" }])
  expect(client.getQueryData(serverKeys.session("chat-a"))).toEqual({
    ...detail,
    session: { ...session, model_id: "new" },
  })
  expect(
    client.getQueryData(serverKeys.session("chat-b")).session.model_id
  ).toBe("other")
  expect(client.getQueryData(serverKeys.settings).selected_model_id).toBe(
    "default"
  )
  expect(client.getQueryData(serverKeys.sessions)[0].model_id).toBe("new")
  disposeSessionQueryClient(client)
})

test("an unsuccessful model mutation preserves the saved preference and is never retried", async () => {
  const { sessionModelMutationOptions, serverKeys } = await helpers()
  const client = createSessionQueryClient()
  const detail = { session: { id: "chat-a", model_id: "old" }, messages: [] }
  client.setQueryData(serverKeys.session("chat-a"), detail)
  let calls = 0
  const mutation = client.getMutationCache().build(
    client,
    sessionModelMutationOptions(client, {
      selectSessionModel: async () => {
        calls += 1
        throw new Error("Save unavailable")
      },
    })
  )
  await expect(
    mutation.execute({ sessionId: "chat-a", modelId: "new" })
  ).rejects.toThrow("Save unavailable")
  expect(calls).toBe(1)
  expect(client.getQueryData(serverKeys.session("chat-a"))).toBe(detail)
  disposeSessionQueryClient(client)
})

test("model preference mutation updates paged chat metadata without changing bounded transcript rows", async () => {
  const { sessionModelMutationOptions, serverKeys } = await helpers()
  const client = createSessionQueryClient()
  const old = { id: "chat-a", model_id: "old" }
  const history = {
    pages: [
      {
        session: old,
        messages: [{ id: "answer" }],
        assistant_outputs: [{ id: "partial" }],
        timeline_order: [{ kind: "assistant_output", id: "partial" }],
        next_before_cursor: "older",
      },
    ],
    pageParams: [null],
  }
  client.setQueryData(serverKeys.history("chat-a"), history)
  client.setQueryData(serverKeys.sessionPages, {
    pages: [{ items: [old], next_before_cursor: null }],
    pageParams: [null],
  })
  const mutation = client.getMutationCache().build(
    client,
    sessionModelMutationOptions(client, {
      selectSessionModel: async () => ({ ...old, model_id: "new" }),
    })
  )
  await mutation.execute({ sessionId: "chat-a", modelId: "new" })
  expect(client.getQueryData(serverKeys.history("chat-a")).pages[0]).toEqual({
    ...history.pages[0],
    session: { ...old, model_id: "new" },
  })
  expect(
    client.getQueryData(serverKeys.sessionPages).pages[0].items[0].model_id
  ).toBe("new")
  disposeSessionQueryClient(client)
})

test("a failed model save resumes a cancelled in-flight chat list read", async () => {
  const { InfiniteQueryObserver } = await import("@tanstack/react-query")
  const { sessionPagesQueryOptions, sessionModelMutationOptions, serverKeys } =
    await helpers()
  const client = createSessionQueryClient()
  let calls = 0
  let firstSignal
  const observer = new InfiniteQueryObserver(
    client,
    sessionPagesQueryOptions({
      listSessionPages: async (_cursor, { signal }) => {
        calls += 1
        if (calls === 1) {
          firstSignal = signal
          return new Promise(() => {})
        }
        return {
          items: [{ id: "chat-a", model_id: "old" }],
          next_before_cursor: null,
        }
      },
    })
  )
  const unsubscribe = observer.subscribe(() => {})
  await Promise.resolve()
  const mutation = client.getMutationCache().build(
    client,
    sessionModelMutationOptions(client, {
      selectSessionModel: async () => {
        throw new Error("Save unavailable")
      },
    })
  )
  await expect(
    mutation.execute({ sessionId: "chat-a", modelId: "new" })
  ).rejects.toThrow("Save unavailable")
  expect(firstSignal.aborted).toBe(true)
  expect(calls).toBe(2)
  expect(
    client.getQueryData(serverKeys.sessionPages).pages[0].items[0].model_id
  ).toBe("old")
  unsubscribe()
  disposeSessionQueryClient(client)
})

test("model saves across chat navigation preserve selection order and the latest pending choice", async () => {
  const { MutationObserver } = await import("@tanstack/react-query")
  const {
    sessionModelMutationOptions,
    sessionModelMutationFilters,
    serverKeys,
  } = await helpers()
  const client = createSessionQueryClient()
  const nextAccount = createSessionQueryClient()
  const pending = ["first", "other", "latest"].map((modelId) => {
    let release
    return {
      modelId,
      wait: new Promise((resolve) => (release = resolve)),
      release: () => release(),
    }
  })
  const started = []
  const saved = new Map()
  const service = {
    selectSessionModel: async (sessionId, modelId) => {
      started.push({ sessionId, modelId })
      await pending.find((request) => request.modelId === modelId).wait
      saved.set(sessionId, modelId)
      return { id: sessionId, model_id: modelId }
    },
  }
  for (const sessionId of ["chat-a", "chat-b"])
    client.setQueryData(serverKeys.history(sessionId), {
      pages: [{ session: { id: sessionId, model_id: "old" }, messages: [] }],
      pageParams: [null],
    })
  const observers = pending.map(
    () =>
      new MutationObserver(client, sessionModelMutationOptions(client, service))
  )
  const unsubscribe = observers.map((observer) => observer.subscribe(() => {}))
  const writes = observers.map((observer, index) =>
    observer.mutate({
      sessionId: index === 1 ? "chat-b" : "chat-a",
      modelId: pending[index].modelId,
    })
  )
  const nextTurn = () => new Promise((resolve) => setTimeout(resolve, 0))
  try {
    await nextTurn()
    expect(started).toEqual([{ sessionId: "chat-a", modelId: "first" }])
    expect(observers[2].getCurrentResult()).toMatchObject({
      isPending: true,
      isPaused: true,
      variables: { sessionId: "chat-a", modelId: "latest" },
    })
    const savesForChatA = sessionModelMutationFilters("chat-a")
    expect(client.isMutating(savesForChatA)).toBe(2)
    expect(client.isMutating(sessionModelMutationFilters(null))).toBe(0)
    expect(client.isMutating(sessionModelMutationFilters("chat-c"))).toBe(0)
    expect(nextAccount.isMutating(savesForChatA)).toBe(0)
    pending[0].release()
    await writes[0]
    await nextTurn()
    expect(started).toEqual([
      { sessionId: "chat-a", modelId: "first" },
      { sessionId: "chat-b", modelId: "other" },
    ])
    expect(observers[2].getCurrentResult().isPending).toBe(true)
    expect(client.isMutating(savesForChatA)).toBe(1)
    pending[1].release()
    await writes[1]
    await nextTurn()
    expect(started).toEqual([
      { sessionId: "chat-a", modelId: "first" },
      { sessionId: "chat-b", modelId: "other" },
      { sessionId: "chat-a", modelId: "latest" },
    ])
    pending[2].release()
    await writes[2]
    expect(saved.get("chat-a")).toBe("latest")
    expect(saved.get("chat-b")).toBe("other")
    expect(
      client.getQueryData(serverKeys.history("chat-a")).pages[0].session
        .model_id
    ).toBe("latest")
    expect(observers[2].getCurrentResult().isPending).toBe(false)
    expect(client.isMutating(savesForChatA)).toBe(0)
  } finally {
    for (const request of pending) request.release()
    await Promise.allSettled(writes)
    for (const dispose of unsubscribe) dispose()
    disposeSessionQueryClient(client)
    disposeSessionQueryClient(nextAccount)
  }
})

test("a history read begun during a model save cannot overwrite its acknowledged preference", async () => {
  const { MutationObserver } = await import("@tanstack/react-query")
  const {
    sessionHistoryQueryOptions,
    sessionModelMutationOptions,
    sessionModelMutationFilters,
    serverKeys,
  } = await helpers()
  const { selectedChatModelId } =
    await import("../apps/web/src/lib/model-choice.ts")
  const client = createSessionQueryClient()
  const staleSession = { id: "chat-a", model_id: "old" }
  let releaseSave
  let releaseRead
  const saving = new Promise((resolve) => (releaseSave = resolve))
  const reading = new Promise((resolve) => (releaseRead = resolve))
  const observer = new MutationObserver(
    client,
    sessionModelMutationOptions(client, {
      selectSessionModel: async () => {
        await saving
        return { ...staleSession, model_id: "saved" }
      },
    })
  )
  const unsubscribe = observer.subscribe(() => {})
  const nextTurn = () => new Promise((resolve) => setTimeout(resolve, 0))
  const write = observer.mutate({ sessionId: "chat-a", modelId: "saved" })
  await nextTurn()
  const history = client.fetchInfiniteQuery(
    sessionHistoryQueryOptions("chat-a", {
      getSessionHistory: async () => {
        await reading
        return {
          session: staleSession,
          messages: [],
          assistant_outputs: [],
          timeline_order: [],
          next_before_cursor: null,
        }
      },
    })
  )
  try {
    releaseSave()
    await nextTurn()
    expect(client.isMutating(sessionModelMutationFilters("chat-a"))).toBe(1)
    releaseRead()
    await Promise.all([write, history])
    const cached = client.getQueryData(serverKeys.history("chat-a"))
    expect(cached.pages[0].session.model_id).toBe("saved")
    expect(
      selectedChatModelId(staleSession, null, null, cached.pages[0].session)
    ).toBe("saved")
    expect(client.isMutating(sessionModelMutationFilters("chat-a"))).toBe(0)
  } finally {
    releaseSave()
    releaseRead()
    await Promise.allSettled([write, history])
    unsubscribe()
    disposeSessionQueryClient(client)
  }
})
