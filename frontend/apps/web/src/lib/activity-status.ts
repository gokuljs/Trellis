import type { OrbState } from "thinking-orbs"

import type { RunSummary } from "@/lib/app-types"
import type { RuntimeRunEvent } from "@/lib/runtime-client"

export type ActivityStatus = {
  label: string
  orb: OrbState
  duration: string | null
  terminal: boolean
}

type StatusCopy = Pick<ActivityStatus, "label" | "orb">

const phaseStates = new Map<string, StatusCopy>([
  ["answering", { label: "Answering", orb: "composing" }],
  ["tool_use", { label: "Using tools", orb: "weaving" }],
  ["waiting", { label: "Waiting", orb: "listening" }],
  ["finishing", { label: "Finishing", orb: "shaping" }],
  ["compacting", { label: "Compacting conversation", orb: "shaping" }],
  ["web_search", { label: "Searching sources", orb: "searching" }],
  ["web_read", { label: "Reading evidence", orb: "searching" }],
  ["web_crawl", { label: "Collecting site pages", orb: "weaving" }],
  ["scoping", { label: "Understanding the question", orb: "working" }],
  ["planning", { label: "Planning the research", orb: "working" }],
  ["researching", { label: "Researching subquestions", orb: "searching" }],
  ["collecting", { label: "Gathering evidence", orb: "searching" }],
  ["comparing", { label: "Comparing sources", orb: "solving" }],
  [
    "checking_conflicts",
    { label: "Checking conflicting claims", orb: "solving" },
  ],
  ["writing", { label: "Preparing the report", orb: "composing" }],
  ["drafting", { label: "Preparing the report", orb: "composing" }],
  ["reviewing", { label: "Checking cited claims", orb: "solving" }],
  ["checking_citations", { label: "Checking cited claims", orb: "solving" }],
  ["repairing", { label: "Revising the report", orb: "shaping" }],
  [
    "waiting_for_input",
    { label: "Waiting for your clarification", orb: "listening" },
  ],
])

const terminalStates = new Map<string, StatusCopy>([
  ["completed", { label: "Completed", orb: "breathing" }],
  ["failed", { label: "Failed", orb: "shaping" }],
  ["cancelled", { label: "Cancelled", orb: "shaping" }],
  ["interrupted", { label: "Interrupted", orb: "shaping" }],
])

const summaryEvents: Record<string, string> = {
  queued: "run.queued",
  recovering: "run.recovering",
  waiting_for_approval: "tool.approval_requested",
  waiting_for_input: "research.clarification_requested",
  cancelling: "run.cancellation_requested",
  completed: "run.completed",
  failed: "run.failed",
  cancelled: "run.cancelled",
  interrupted: "run.interrupted",
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

function currentState(event: RuntimeRunEvent): StatusCopy | null {
  switch (event.eventType) {
    case "run.phase":
      if (typeof event.data.label === "string" && event.data.label.trim())
        return {
          label: event.data.label.slice(0, 200),
          orb: phaseStates.get(String(event.data.phase))?.orb ?? "working",
        }
      return typeof event.data.phase === "string"
        ? (phaseStates.get(event.data.phase) ?? null)
        : null
    case "run.queued":
      return { label: "Waiting", orb: "connecting" }
    case "run.recovering":
      return { label: "Recovering", orb: "connecting" }
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
      return { label: "Waiting for approval", orb: "listening" }
    case "research.clarification_requested":
      return { label: "Waiting for your clarification", orb: "listening" }
    case "research.clarification_answered":
      return { label: "Continuing research", orb: "working" }
    case "tool.approval_decided":
      return { label: "Thinking", orb: "working" }
    case "run.cancellation_requested":
      return { label: "Stopping", orb: "shaping" }
    case "run.completed":
    case "run.failed":
    case "run.cancelled":
    case "run.interrupted":
      return terminalStates.get(event.eventType.slice(4)) ?? null
    default:
      return null
  }
}

export function describeActivity(
  events: RuntimeRunEvent[],
  pending: boolean,
  now: number,
  latestEvent?: RuntimeRunEvent,
  summary?: RunSummary,
  confirmedStatus?: string
): ActivityStatus {
  const confirmedTerminal = terminalStates.get(confirmedStatus ?? "")
  const newestSequence = Math.max(
    0,
    latestEvent?.sequence ?? 0,
    ...events.map((event) => event.sequence)
  )
  if (
    confirmedTerminal &&
    !(
      summary &&
      summary.status === confirmedStatus &&
      summary.last_sequence >= newestSequence
    ) &&
    ![...events, ...(latestEvent ? [latestEvent] : [])].some(
      (event) => event.eventType === `run.${confirmedStatus}`
    )
  ) {
    return { ...confirmedTerminal, duration: null, terminal: true }
  }
  if (events.length === 0 && !latestEvent) {
    if (summary) {
      const eventType = summaryEvents[summary.status] ?? "run.started"
      const terminal = terminalEvents.has(eventType)
      const start = Date.parse(summary.created_at)
      const finish =
        terminal && summary.finished_at ? Date.parse(summary.finished_at) : now
      return {
        ...(currentState({
          runId: summary.run_id,
          sequence: summary.last_sequence,
          eventType,
          eventVersion: 1,
          data: {},
        }) ?? { label: "Thinking", orb: "working" }),
        duration:
          summary.status === "waiting_for_input"
            ? null
            : Number.isFinite(start) && Number.isFinite(finish)
              ? elapsedLabel(finish - start)
              : null,
        terminal,
      }
    }
    if (confirmedStatus === "recovering")
      return {
        label: "Recovering",
        orb: "connecting",
        duration: null,
        terminal: false,
      }
    if (confirmedStatus === "waiting_for_input")
      return {
        label: "Waiting for your clarification",
        orb: "listening",
        duration: null,
        terminal: false,
      }
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
  const matchingSummary = summary?.run_id === latest.runId ? summary : undefined
  const summaryIsCurrent = Boolean(
    matchingSummary && matchingSummary.last_sequence >= latest.sequence
  )
  const terminalEvent = ordered.findLast((event) =>
    terminalEvents.has(event.eventType)
  )
  const summaryEvent = matchingSummary
    ? summaryEvents[matchingSummary.status]
    : undefined
  const summaryIsTerminal =
    summaryIsCurrent &&
    summaryEvent !== undefined &&
    terminalEvents.has(summaryEvent)
  const terminal = Boolean(terminalEvent || summaryIsTerminal)
  const queued = ordered.find((event) => event.eventType === "run.queued")
  const startedAt =
    queued?.createdAt ?? matchingSummary?.created_at ?? ordered[0].createdAt
  const start = startedAt ? Date.parse(startedAt) : Number.NaN
  const terminalAt =
    summaryIsTerminal &&
    (!terminalEvent || matchingSummary!.last_sequence >= terminalEvent.sequence)
      ? matchingSummary!.finished_at
      : terminalEvent?.createdAt
  const finish = terminal
    ? terminalAt
      ? Date.parse(terminalAt)
      : Number.NaN
    : now
  const duration =
    Number.isFinite(start) && Number.isFinite(finish)
      ? elapsedLabel(finish - start)
      : null
  let state: StatusCopy = { label: "Thinking", orb: "working" }
  let hasPhase = false
  let stopping = false
  let awaitingApproval = false
  for (const event of ordered) {
    if (event.eventType === "run.cancellation_requested") {
      stopping = true
      state = { label: "Stopping", orb: "shaping" }
      continue
    }
    if (stopping) continue
    if (
      event.eventType === "run.recovering" ||
      event.eventType === "run.resumed"
    ) {
      awaitingApproval = false
      hasPhase = false
      state = currentState(event)!
      continue
    }
    if (
      event.eventType === "tool.approval_requested" ||
      event.eventType === "research.clarification_requested"
    ) {
      awaitingApproval = true
      state = currentState(event)!
      continue
    }
    if (
      event.eventType === "tool.approval_decided" ||
      event.eventType === "research.clarification_answered"
    ) {
      awaitingApproval = false
      state = { label: "Waiting", orb: "working" }
      continue
    }
    if (awaitingApproval) continue
    const incoming = currentState(event)
    if (event.eventType === "run.phase") {
      if (incoming) {
        state = incoming
        hasPhase = true
      }
    } else if (!hasPhase && incoming) state = incoming
  }
  if (summaryIsCurrent && matchingSummary?.status === "recovering")
    state = { label: "Recovering", orb: "connecting" }
  if (summaryIsCurrent && matchingSummary?.status === "cancelling")
    state = { label: "Stopping", orb: "shaping" }
  if (summaryIsCurrent && matchingSummary?.status === "waiting_for_input")
    state = { label: "Waiting for your clarification", orb: "listening" }
  if (
    matchingSummary?.status === "waiting_for_approval" &&
    summaryIsCurrent &&
    !ordered.some(
      (event) =>
        event.sequence > matchingSummary.last_sequence &&
        (event.eventType === "tool.approval_decided" ||
          (event.eventType === "run.phase" && currentState(event) !== null))
    )
  )
    state = { label: "Waiting for approval", orb: "listening" }
  if (terminalEvent) state = currentState(terminalEvent)!
  if (
    summaryIsTerminal &&
    (!terminalEvent || matchingSummary!.last_sequence >= terminalEvent.sequence)
  ) {
    state = currentState({
      runId: latest.runId,
      sequence: matchingSummary!.last_sequence,
      eventType: summaryEvent!,
      eventVersion: 1,
      data: {},
    })!
  }
  return {
    ...state,
    duration:
      state.label === "Waiting for your clarification" ? null : duration,
    terminal,
  }
}
