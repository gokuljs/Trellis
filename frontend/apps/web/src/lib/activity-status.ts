import type { OrbState } from "thinking-orbs"

import type { RuntimeRunEvent } from "@/lib/runtime-client"

export type ActivityStatus = {
  label: string
  orb: OrbState
  duration: string | null
  terminal: boolean
}

const terminalEvents = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.interrupted",
])

function elapsedLabel(milliseconds: number): string {
  const seconds = Math.floor(Math.max(0, milliseconds) / 1000)
  const minutes = Math.floor(seconds / 60)
  if (minutes === 0) return `${seconds}s`
  const hours = Math.floor(minutes / 60)
  if (hours === 0)
    return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`
}

function currentState(
  event: RuntimeRunEvent
): Pick<ActivityStatus, "label" | "orb"> {
  switch (event.eventType) {
    case "run.queued":
      return { label: "Queued", orb: "connecting" }
    case "run.started":
    case "run.resumed":
      return { label: "Thinking", orb: "working" }
    case "model.usage":
    case "model.completed":
      return { label: "Computing", orb: "solving" }
    case "assistant.delta":
    case "assistant.message":
    case "assistant.completed":
      return { label: "Composing", orb: "composing" }
    case "tool.call": {
      const name = event.data.name
      return typeof name === "string" &&
        /search|find|list|grep|glob|browse|fetch/i.test(name)
        ? { label: "Searching", orb: "searching" }
        : { label: "Using tools", orb: "weaving" }
    }
    case "tool.result":
      return { label: "Using tools", orb: "weaving" }
    case "tool.approval_requested":
      return { label: "Needs approval", orb: "listening" }
    case "tool.approval_decided":
      return { label: "Thinking", orb: "working" }
    case "run.cancellation_requested":
      return { label: "Stopping", orb: "shaping" }
    case "run.completed":
      return { label: "Completed", orb: "breathing" }
    case "run.failed":
      return { label: "Failed", orb: "shaping" }
    case "run.cancelled":
      return { label: "Cancelled", orb: "shaping" }
    case "run.interrupted":
      return { label: "Interrupted", orb: "shaping" }
    default:
      return { label: "Thinking", orb: "working" }
  }
}

export function describeActivity(
  events: RuntimeRunEvent[],
  pending: boolean,
  now: number,
  latestEvent?: RuntimeRunEvent
): ActivityStatus {
  if (events.length === 0 && !latestEvent) {
    return pending
      ? { label: "Thinking", orb: "working", duration: null, terminal: false }
      : {
          label: "Activity unavailable",
          orb: "breathing",
          duration: null,
          terminal: true,
        }
  }

  const ordered = [...events, ...(latestEvent ? [latestEvent] : [])].sort(
    (left, right) => left.sequence - right.sequence
  )
  const latest = ordered.at(-1)!
  const terminal = terminalEvents.has(latest.eventType)
  const queued =
    ordered.find((event) => event.eventType === "run.queued") ?? ordered[0]
  const start = queued.createdAt ? Date.parse(queued.createdAt) : Number.NaN
  const finish =
    terminal && latest.createdAt ? Date.parse(latest.createdAt) : now
  const duration =
    Number.isFinite(start) && Number.isFinite(finish)
      ? elapsedLabel(finish - start)
      : null

  return { ...currentState(latest), duration, terminal }
}
