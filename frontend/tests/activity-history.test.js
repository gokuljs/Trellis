import { expect, test } from "bun:test"
import { InfiniteQueryObserver } from "@tanstack/react-query"
import {
  createSessionQueryClient,
  disposeSessionQueryClient,
} from "../apps/web/src/lib/query-client.ts"
import {
  activitiesFromSummaries,
  eventsFromPages,
} from "../apps/web/src/lib/activity-history.ts"
import {
  runEventsQueryOptions,
  runSummariesQueryOptions,
} from "../apps/web/src/lib/server-queries.ts"

test("descending summary pages retain chronological attempts and the newest run status", () => {
  const latest = {
    run_id: "new",
    turn_id: "turn",
    created_at: "2026-10-15T02:00:00Z",
    status: "completed",
  }
  const older = {
    ...latest,
    run_id: "old",
    created_at: "2026-10-15T01:00:00Z",
    status: "failed",
  }
  const activity = activitiesFromSummaries([latest, older, latest]).turn
  expect(activity.summary.run_id).toBe("new")
  expect(activity.priorAttempts.map((item) => item.runId)).toEqual(["old"])
})

test("saved activity is paged and duplicate event sequences appear once", () => {
  const event = (sequence, run_id = "run") => ({
    sequence,
    run_id,
    event_type: "assistant.delta",
    event_version: 1,
    data: {},
    created_at: "now",
  })
  expect(
    eventsFromPages(
      [
        { items: [event(2), event(1)] },
        { items: [event(2), event(3), event(4, "other")] },
      ],
      "run"
    ).map((item) => item.sequence)
  ).toEqual([1, 2, 3])
})

for (const kind of ["events", "summaries"])
  test(`fifty ${kind} pages keep five pages and propagate cancellation`, async () => {
    const client = createSessionQueryClient()
    const options =
      kind === "events"
        ? runEventsQueryOptions("chat", "run", {
            listRunEvents: async (_chat, _run, after, { signal }) => {
              expect(signal).toBeInstanceOf(AbortSignal)
              return { items: [], next_after_sequence: after + 100 }
            },
          })
        : runSummariesQueryOptions("chat", {
            listRunSummaries: async (_chat, offset, { signal }) => {
              expect(signal).toBeInstanceOf(AbortSignal)
              return { items: [], next_offset: offset + 100 }
            },
          })
    const observer = new InfiniteQueryObserver(client, {
      ...options,
      enabled: false,
    })
    const unsubscribe = observer.subscribe(() => {})
    await observer.refetch()
    for (let index = 0; index < 49; index += 1) await observer.fetchNextPage()
    expect(observer.getCurrentResult().data.pages).toHaveLength(5)
    expect(observer.getCurrentResult().data.pageParams[0]).toBe(4500)
    unsubscribe()
    disposeSessionQueryClient(client)
  })

test("repeated retries retain bounded attempt metadata without retaining old streamed event arrays", async () => {
  const { startNextAttempt } =
    await import("../apps/web/src/lib/activity-history.ts")
  let activity
  for (let index = 0; index < 1000; index += 1) {
    activity = startNextAttempt({
      ...activity,
      runInfo: { runId: String(index) },
      events: [{ data: { text: "large streamed output" } }],
    })
  }
  expect(activity.priorAttempts).toHaveLength(500)
  expect(activity.priorAttempts[0].runId).toBe("500")
  expect(
    activity.priorAttempts.every((attempt) => attempt.events === null)
  ).toBe(true)
})

test("closing a saved event disclosure aborts the page request and drops its private cache", async () => {
  const client = createSessionQueryClient()
  let signal
  const options = runEventsQueryOptions("chat", "run", {
    listRunEvents: async (_chat, _run, _after, request) => {
      signal = request.signal
      return new Promise(() => {})
    },
  })
  const observer = new InfiniteQueryObserver(client, options)
  const unsubscribe = observer.subscribe(() => {})
  await Promise.resolve()
  expect(signal.aborted).toBe(false)
  unsubscribe()
  expect(signal.aborted).toBe(true)
  for (
    let index = 0;
    index < 50 && client.getQueryCache().getAll().length;
    index += 1
  )
    await new Promise((resolve) => setTimeout(resolve, 2))
  expect(client.getQueryCache().getAll()).toHaveLength(0)
  disposeSessionQueryClient(client)
})

test("a history window releases completed streamed event bodies and keeps live activity", async () => {
  const { retainWindowActivities } =
    await import("../apps/web/src/lib/activity-history.ts")
  const event = (sequence, eventType) => ({
    runId: "saved",
    sequence,
    eventType,
    data: { text: "large" },
  })
  const saved = {
    runInfo: { runId: "saved" },
    events: [
      event(1, "run.queued"),
      event(2, "assistant.delta"),
      event(3, "run.completed"),
    ],
    latestEvent: event(3, "run.completed"),
  }
  const live = { runInfo: { runId: "live" }, events: [event(1, "run.started")] }
  const retained = retainWindowActivities(
    { saved, live, absent: { events: [], runInfo: null } },
    new Set(["saved"]),
    "live"
  )
  expect(Object.keys(retained)).toEqual(["saved", "live"])
  expect(retained.saved.events.map((item) => item.eventType)).toEqual([
    "run.queued",
    "run.completed",
  ])
  expect(retained.live).toBe(live)
})
