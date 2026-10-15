import { expect, test } from "bun:test"
import { InfiniteQueryObserver } from "@tanstack/react-query"
import {
  createSessionQueryClient,
  disposeSessionQueryClient,
} from "../apps/web/src/lib/query-client.ts"
import {
  threadMessages,
  newAssistantOutputState,
  mergeSavedAssistantOutputs,
} from "../apps/web/src/lib/assistant-outputs.ts"
import {
  flattenHistoryPages,
  hydrateOutputWindow,
  retryInputForRun,
} from "../apps/web/src/lib/history-window.ts"
import {
  sessionHistoryQueryOptions,
  resetHistoryToLatest,
} from "../apps/web/src/lib/server-queries.ts"

const session = { id: "chat", title: "Chat" }
const message = (id, turn_id = id) => ({
  id,
  turn_id,
  role: "user",
  content: id,
  created_at: id,
})
const output = (id, run_id = id) => ({
  id,
  turn_id: "turn",
  run_id,
  model_call_id: id,
  step_index: 1,
  content: id,
  status: "partial",
  promoted_message_id: null,
  created_at: id,
})
const page = (
  messages,
  outputs,
  timeline_order,
  next_before_cursor = null
) => ({
  session,
  messages,
  assistant_outputs: outputs,
  timeline_order,
  next_before_cursor,
})

test("history pages follow server timeline order including partials whose user row is outside the window", () => {
  const older = page(
    [message("a")],
    [output("partial")],
    [
      { kind: "message", id: "a" },
      { kind: "assistant_output", id: "partial" },
    ]
  )
  const newest = page(
    [message("b"), message("a")],
    [],
    [
      { kind: "message", id: "a" },
      { kind: "message", id: "b" },
    ]
  )
  const merged = flattenHistoryPages([newest, older])
  expect(merged.messages.map((item) => item.id)).toEqual(["a", "b"])
  expect(
    threadMessages(
      merged.messages,
      merged.assistant_outputs,
      {},
      merged.timeline_order
    ).map((item) => item.id)
  ).toEqual(["a", "output-partial", "b"])
})

test("evicting a history page releases output text and event cursors while preserving the active run", () => {
  const saved = [output("evicted"), output("visible"), output("live", "active")]
  const state = mergeSavedAssistantOutputs(
    newAssistantOutputState(),
    saved.map((item) => ({ ...item, next_batch_index: 2 }))
  )
  state.lastSequence = new Map([
    ["evicted", 10],
    ["visible", 3],
    ["active", 5],
  ])
  const hydrated = hydrateOutputWindow(state, [saved[1]], "active")
  expect(hydrated.outputs.map((item) => item.id)).toEqual(["visible", "live"])
  expect([...hydrated.liveText.keys()]).toEqual(["visible", "live"])
  expect([...hydrated.lastSequence.keys()]).toEqual(["visible", "active"])
})

test("fifty history fetches retain five pages and Jump to latest restarts from the newest cursor", async () => {
  const client = createSessionQueryClient()
  const cursors = []
  const service = {
    getSessionHistory: async (_sessionId, cursor, { signal }) => {
      expect(signal).toBeInstanceOf(AbortSignal)
      cursors.push(cursor)
      const index = cursor === null ? 0 : Number(cursor)
      return page(
        [message(String(index))],
        [],
        [{ kind: "message", id: String(index) }],
        String(index + 1)
      )
    },
  }
  const options = sessionHistoryQueryOptions("chat", service)
  const observer = new InfiniteQueryObserver(client, {
    ...options,
    enabled: false,
  })
  const unsubscribe = observer.subscribe(() => {})
  await observer.refetch()
  for (let index = 0; index < 49; index += 1) await observer.fetchNextPage()
  expect(observer.getCurrentResult().data.pages).toHaveLength(5)
  expect(observer.getCurrentResult().data.pageParams).toEqual([
    "45",
    "46",
    "47",
    "48",
    "49",
  ])
  const latest = await resetHistoryToLatest(client, "chat", service)
  expect(latest.pages).toHaveLength(1)
  expect(latest.pageParams).toEqual([null])
  expect(cursors.at(-1)).toBe(null)
  unsubscribe()
  disposeSessionQueryClient(client)
})

test("latest run input is recovery metadata and does not need to be loaded into the visible history window", () => {
  const input = message("accepted", "current-turn")
  const latest = {
    run_id: "run",
    turn_id: "current-turn",
    status: "failed",
    input_message: input,
  }
  expect(retryInputForRun("chat", [message("different-turn")], latest)).toEqual(
    { sessionId: "chat", turnId: "current-turn", content: "accepted" }
  )
  expect(
    retryInputForRun("chat", [], {
      ...latest,
      input_message: { ...input, turn_id: "wrong" },
    })
  ).toBe(null)
})

test("the sidebar also retains only five pages when browsing a long chat list", async () => {
  const { sessionPagesQueryOptions } =
    await import("../apps/web/src/lib/server-queries.ts")
  const client = createSessionQueryClient()
  const observer = new InfiniteQueryObserver(client, {
    ...sessionPagesQueryOptions({
      listSessionPages: async (cursor) => ({
        items: [{ id: cursor ?? "newest" }],
        next_before_cursor: String(Number(cursor ?? 0) + 1),
      }),
    }),
    enabled: false,
  })
  const unsubscribe = observer.subscribe(() => {})
  await observer.refetch()
  for (let index = 0; index < 49; index += 1) await observer.fetchNextPage()
  expect(observer.getCurrentResult().data.pages).toHaveLength(5)
  expect(observer.getCurrentResult().data.pageParams[0]).toBe("45")
  unsubscribe()
  disposeSessionQueryClient(client)
})

test("a newly created chat resets list pagination instead of dropping a cursor-boundary row", async () => {
  const { sessionPagesQueryOptions, cacheSession } =
    await import("../apps/web/src/lib/server-queries.ts")
  const client = createSessionQueryClient()
  const cursors = []
  const observer = new InfiniteQueryObserver(
    client,
    sessionPagesQueryOptions({
      listSessionPages: async (cursor) => {
        cursors.push(cursor)
        return {
          items: [{ id: cursor ?? "newest" }],
          next_before_cursor: String(Number(cursor ?? 0) + 1),
        }
      },
    })
  )
  const unsubscribe = observer.subscribe(() => {})
  await observer.refetch()
  await observer.fetchNextPage()
  cacheSession(client, { id: "new-chat" })
  await Promise.resolve()
  expect(cursors.at(-1)).toBe(null)
  await observer.refetch()
  expect(observer.getCurrentResult().data.pageParams).toEqual([null])
  unsubscribe()
  disposeSessionQueryClient(client)
})
