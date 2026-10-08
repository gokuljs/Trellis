import type { RuntimeRunEvent } from "@/lib/runtime-client"

export type RunEventGroup = {
  key: string
  title: string
  kind: "run" | "model" | "tool"
  events: RuntimeRunEvent[]
}

function toolName(event: RuntimeRunEvent): string {
  return typeof event.data.name === "string" ? event.data.name : "tool"
}

export function groupRunEvents(events: RuntimeRunEvent[]): RunEventGroup[] {
  const groups: RunEventGroup[] = []
  const tools = new Map<string, RunEventGroup>()
  let model: RunEventGroup | null = null
  let modelComplete = false
  let modelIndex = 0
  let start: RunEventGroup | null = null

  for (const event of [...events].sort(
    (left, right) => left.sequence - right.sequence
  )) {
    if (["assistant.delta", "assistant.completed"].includes(event.eventType))
      continue

    if (
      ["model.usage", "model.completed", "assistant.message"].includes(
        event.eventType
      )
    ) {
      if (!model || (event.eventType === "model.usage" && modelComplete)) {
        modelIndex += 1
        model = {
          key: `model-${event.sequence}`,
          title: `Model step ${modelIndex}`,
          kind: "model",
          events: [],
        }
        groups.push(model)
        modelComplete = false
      }
      model.events.push(event)
      if (event.eventType === "model.completed") modelComplete = true
      continue
    }

    if (event.eventType.startsWith("tool.")) {
      model = null
      const callId =
        typeof event.data.tool_call_id === "string"
          ? event.data.tool_call_id
          : `unknown-${event.sequence}`
      let group = tools.get(callId)
      if (!group) {
        group = {
          key: `tool-${callId}`,
          title: `Tool · ${toolName(event)}`,
          kind: "tool",
          events: [],
        }
        tools.set(callId, group)
        groups.push(group)
      } else if (
        group.title === "Tool · tool" &&
        typeof event.data.name === "string"
      ) {
        group.title = `Tool · ${toolName(event)}`
      }
      group.events.push(event)
      continue
    }

    model = null
    if (event.eventType === "run.queued" || event.eventType === "run.started") {
      if (!start) {
        start = {
          key: `start-${event.sequence}`,
          title: "Start",
          kind: "run",
          events: [],
        }
        groups.push(start)
      }
      start.events.push(event)
      continue
    }
    const title =
      event.eventType === "run.completed"
        ? "Finish"
        : event.eventType === "run.resumed"
          ? "Resume"
          : event.eventType === "run.cancellation_requested"
            ? "Stopping"
            : "Run status"
    groups.push({
      key: `run-${event.sequence}`,
      title,
      kind: "run",
      events: [event],
    })
  }

  return groups
}
