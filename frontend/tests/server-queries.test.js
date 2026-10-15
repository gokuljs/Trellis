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
