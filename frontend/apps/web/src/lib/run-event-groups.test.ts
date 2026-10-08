import { describe, expect, it } from "vitest"

import { groupRunEvents } from "@/lib/run-event-groups"
import type { RuntimeRunEvent } from "@/lib/runtime-client"

function event(
  sequence: number,
  eventType: string,
  toolCallId?: string
): RuntimeRunEvent {
  return {
    runId: "run-1",
    sequence,
    eventType,
    eventVersion: 1,
    data: toolCallId ? { tool_call_id: toolCallId, name: "read_file" } : {},
  }
}

describe("run event groups", () => {
  it("keeps model usage and notes together, and joins each tool result to its request", () => {
    const groups = groupRunEvents([
      event(8, "run.completed"),
      event(3, "model.usage"),
      event(1, "run.queued"),
      event(5, "tool.call", "tool-1"),
      event(4, "assistant.message"),
      event(7, "tool.result", "tool-1"),
      event(6, "tool.approval_requested", "tool-1"),
      event(2, "run.started"),
    ])

    expect(groups.map((group) => group.kind)).toEqual([
      "run",
      "model",
      "tool",
      "run",
    ])
    expect(
      groups.map((group) => group.events.map((item) => item.sequence))
    ).toEqual([[1, 2], [3, 4], [5, 6, 7], [8]])
  })

  it("starts a new model step after a completed step and a tool result", () => {
    const groups = groupRunEvents([
      event(1, "model.usage"),
      event(2, "model.completed"),
      event(3, "tool.call", "tool-1"),
      event(4, "tool.result", "tool-1"),
      event(5, "model.usage"),
      event(6, "model.completed"),
    ])
    expect(groups.map((group) => group.title)).toEqual([
      "Model step 1",
      "Tool · read_file",
      "Model step 2",
    ])
  })
})
