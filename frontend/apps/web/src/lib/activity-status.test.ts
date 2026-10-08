import { describe, expect, it } from "vitest"

import { describeActivity } from "@/lib/activity-status"
import type { RuntimeRunEvent } from "@/lib/runtime-client"

function event(
  sequence: number,
  eventType: string,
  createdAt: string,
  data: Record<string, unknown> = {}
): RuntimeRunEvent {
  return {
    runId: "run-1",
    sequence,
    eventType,
    eventVersion: 1,
    createdAt,
    data,
  }
}

const queued = event(1, "run.queued", "2026-10-07T12:00:00Z")

describe("activity status", () => {
  it.each([
    ["run.started", {}, "Thinking", "working"],
    ["model.usage", {}, "Computing", "solving"],
    ["tool.call", { name: "search_files" }, "Searching", "searching"],
    ["tool.call", { name: "run_command" }, "Using tools", "weaving"],
    ["assistant.message", {}, "Composing", "composing"],
    ["tool.approval_requested", {}, "Needs approval", "listening"],
    ["run.failed", {}, "Failed", "shaping"],
    ["run.completed", {}, "Completed", "breathing"],
  ])("maps %s to %s activity", (eventType, data, label, orb) => {
    const state = describeActivity(
      [queued, event(2, eventType, "2026-10-07T12:00:10Z", data)],
      true,
      Date.parse("2026-10-07T12:00:20Z")
    )
    expect(state.label).toBe(label)
    expect(state.orb).toBe(orb)
  })

  it("measures the entire attempt from queued to its terminal event", () => {
    expect(
      describeActivity(
        [queued, event(2, "run.completed", "2026-10-07T12:01:05Z")],
        false,
        Date.parse("2026-10-07T13:00:00Z")
      ).duration
    ).toBe("1m 05s")
  })

  it("keeps elapsed time running for an active attempt", () => {
    expect(
      describeActivity(
        [queued, event(2, "run.started", "2026-10-07T12:00:02Z")],
        true,
        Date.parse("2026-10-07T12:00:09Z")
      ).duration
    ).toBe("9s")
  })

  it("shows unavailable without an invented duration when no run was recorded", () => {
    expect(describeActivity([], false, Date.now())).toMatchObject({
      label: "Activity unavailable",
      duration: null,
    })
  })

  it("uses a saved summary before its events have been fetched", () => {
    const status = describeActivity(
      [],
      false,
      Date.parse("2026-10-07T13:00:00Z"),
      undefined,
      {
        run_id: "run-1",
        turn_id: "turn-1",
        retry_of: null,
        status: "completed",
        budget_preset: "conservative",
        max_model_calls: 8,
        max_tool_calls: 16,
        max_total_tokens: 100000,
        max_cost_usd: 2,
        deadline_at: "2026-10-07T12:10:00Z",
        last_sequence: 7,
        created_at: "2026-10-07T12:00:00Z",
        started_at: "2026-10-07T12:00:01Z",
        finished_at: "2026-10-07T12:00:12Z",
      }
    )
    expect(status).toMatchObject({
      label: "Completed",
      duration: "12s",
      terminal: true,
    })
  })
})
