import { expect, test } from "bun:test"
import { describeActivity } from "../apps/web/src/lib/activity-status.ts"

function event(sequence, eventType, data = {}) {
  return {
    runId: "run-a",
    sequence,
    eventType,
    eventVersion: 1,
    data,
    createdAt: `2026-10-15T08:00:${String(sequence).padStart(2, "0")}Z`,
  }
}

for (const [phase, label] of [
  ["answering", "Answering"],
  ["tool_use", "Using tools"],
  ["waiting", "Waiting"],
  ["finishing", "Finishing"],
  ["compacting", "Compacting conversation"],
]) {
  test(`${phase} stays stable while usage and text events arrive`, () => {
    const events = [
      event(1, "run.queued"),
      event(2, "run.phase", { phase }),
      event(3, "model.usage"),
      event(4, "assistant.delta", { text: "Text" }),
      event(5, "model.completed"),
    ]
    const status = describeActivity(
      events,
      true,
      Date.parse("2026-10-15T08:00:10Z")
    )
    expect(status.label).toBe(label)
    expect(status.terminal).toBe(false)
  })
}

test("unknown future phases and events do not replace the last known phase", () => {
  const events = [
    event(1, "run.phase", { phase: "answering" }),
    event(2, "run.phase", { phase: "future_phase" }),
    event(3, "future.event"),
  ]
  expect(describeActivity(events, true, Date.now()).label).toBe("Answering")
})

test("queued work and approval pauses explain what the agent is waiting for", () => {
  expect(
    describeActivity([event(1, "run.queued")], true, Date.now()).label
  ).toBe("Waiting")
  const events = [
    event(1, "run.phase", { phase: "tool_use" }),
    event(2, "tool.approval_requested"),
    event(3, "model.usage"),
  ]
  expect(describeActivity(events, true, Date.now()).label).toBe(
    "Waiting for approval"
  )
  events.push(
    event(4, "tool.approval_decided"),
    event(5, "run.phase", { phase: "tool_use" })
  )
  expect(describeActivity(events, true, Date.now()).label).toBe("Using tools")
})

test("cancellation cannot be hidden by a late phase or usage event", () => {
  const events = [
    event(1, "run.phase", { phase: "answering" }),
    event(2, "run.cancellation_requested"),
    event(3, "model.usage"),
    event(4, "run.phase", { phase: "finishing" }),
  ]
  expect(describeActivity(events, true, Date.now()).label).toBe("Stopping")
})

for (const [terminal, label] of [
  ["completed", "Completed"],
  ["failed", "Failed"],
  ["cancelled", "Cancelled"],
  ["interrupted", "Interrupted"],
]) {
  test(`${terminal} overrides telemetry and fixes the elapsed duration`, () => {
    const events = [
      event(1, "run.queued"),
      event(2, "run.phase", { phase: "finishing" }),
      event(3, `run.${terminal}`),
      event(4, "model.usage"),
    ]
    const status = describeActivity(
      events,
      false,
      Date.parse("2026-10-15T08:00:20Z")
    )
    expect(status.label).toBe(label)
    expect(status.terminal).toBe(true)
    expect(status.duration).toBe("2s")
  })
}

test("a terminal saved run summary overrides incompletely loaded activity", () => {
  const events = [event(2, "run.phase", { phase: "answering" })]
  const summary = {
    run_id: "run-a",
    status: "completed",
    last_sequence: 5,
    created_at: "2026-10-15T08:00:00Z",
    finished_at: "2026-10-15T08:00:05Z",
  }
  const status = describeActivity(events, false, Date.now(), undefined, summary)
  expect(status.label).toBe("Completed")
  expect(status.terminal).toBe(true)
  expect(status.duration).toBe("5s")
})

test("ordered replay and duplicate latest-event delivery produce the same stable phase", () => {
  const phase = event(3, "run.phase", { phase: "tool_use" })
  const events = [
    phase,
    event(1, "run.queued"),
    event(2, "run.phase", { phase: "answering" }),
    event(4, "tool.result"),
  ]
  expect(describeActivity(events, true, Date.now(), phase).label).toBe(
    "Using tools"
  )
})

test("a newer valid phase supersedes an older saved approval pause", () => {
  const summary = {
    run_id: "run-a",
    status: "waiting_for_approval",
    last_sequence: 2,
    created_at: "2026-10-15T08:00:00Z",
    finished_at: null,
  }
  const phase = event(3, "run.phase", { phase: "tool_use" })
  expect(describeActivity([], true, Date.now(), phase, summary).label).toBe(
    "Using tools"
  )
})

test("unknown phases cannot hide a saved approval pause", () => {
  const summary = {
    run_id: "run-a",
    status: "waiting_for_approval",
    last_sequence: 2,
    created_at: "2026-10-15T08:00:00Z",
    finished_at: null,
  }
  const phase = event(3, "run.phase", { phase: "future_phase" })
  expect(describeActivity([], true, Date.now(), phase, summary).label).toBe(
    "Waiting for approval"
  )
})
