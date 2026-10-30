import type { LatestRun, Message } from "@/lib/app-types"

import type {
  RuntimeRunEvent,
  RuntimeRunInfo,
  TurnRunActivity,
} from "@/lib/runtime-client"

export type TerminalRunOutcome =
  "completed" | "failed" | "cancelled" | "interrupted"

export type RunOwner = { sessionId: string | null; generation: number }

export function ownsRun(owner: RunOwner, current: RunOwner): boolean {
  return (
    owner.sessionId === current.sessionId &&
    owner.generation === current.generation
  )
}

export function isReconciledRunOutcome(
  status: string | null | undefined
): boolean {
  return ["completed", "cancelled", "interrupted"].includes(status ?? "")
}

export function turnFailureAction(
  code: string | undefined,
  terminalOutcome: TerminalRunOutcome | null,
  hasAcceptedRun = false
): "reconnect" | "reload" | "retry" {
  if (terminalOutcome === "completed") return "reload"
  if (terminalOutcome !== null) return "retry"
  if (hasAcceptedRun) return "reconnect"
  return [
    "message_empty",
    "message_too_large",
    "run_invalid",
    "invalid_budget_preset",
    "model_not_available",
    "model_not_streaming",
    "provider_not_available",
    "provider_not_configured",
    "research_unavailable",
    "research_question_too_large",
    "invalid_run_mode",
    "run_capacity",
    "run_token_too_short",
    "session_not_found",
    "rpc_invalid_params",
  ].includes(code ?? "")
    ? "retry"
    : "reconnect"
}

export function recoveryTranscript(
  messages: Message[],
  turn: { turnId: string; content: string }
): Message[] {
  if (
    messages.some(
      (message) => message.role === "user" && message.turn_id === turn.turnId
    )
  )
    return messages
  return [
    ...messages,
    {
      id: `pending-${turn.turnId}`,
      turn_id: turn.turnId,
      role: "user",
      content: turn.content,
      provider: null,
      model: null,
      created_at: new Date().toISOString(),
    },
  ]
}

export function recoveryRunInfo(run: LatestRun): RuntimeRunInfo {
  return {
    runId: run.run_id,
    runMode: run.run_mode,
    status: run.status,
    retryAllowed: run.retry_allowed,
    recoveryReason: run.recovery_reason,
    uncertainToolCallId: run.uncertain_tool_call_id,
  }
}

export function mergeRunInfo(
  previous: RuntimeRunInfo | null | undefined,
  incoming: RuntimeRunInfo
): RuntimeRunInfo {
  if (previous?.runId !== incoming.runId) return incoming
  return {
    ...previous,
    ...incoming,
    ...(previous.retryAllowed === false ? { retryAllowed: false } : {}),
  }
}

export function runInfoFromEvent(
  previous: RuntimeRunInfo | null,
  event: RuntimeRunEvent
): RuntimeRunInfo {
  const incoming: RuntimeRunInfo = { runId: event.runId }
  const statuses: Record<string, string> = {
    "run.queued": "queued",
    "run.started": "running",
    "run.recovering": "recovering",
    "run.resumed": "running",
    "run.cancellation_requested": "cancelling",
    "tool.approval_requested": "waiting_for_approval",
    "tool.approval_decided": "running",
    "research.clarification_requested": "waiting_for_input",
    "research.clarification_answered": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
  }
  if (statuses[event.eventType]) incoming.status = statuses[event.eventType]
  if (event.data.run_mode === "research" || event.data.run_mode === "chat")
    incoming.runMode = event.data.run_mode
  if (typeof event.data.retry_allowed === "boolean")
    incoming.retryAllowed = event.data.retry_allowed
  if (
    typeof event.data.recovery_reason === "string" ||
    event.data.recovery_reason === null
  )
    incoming.recoveryReason = event.data.recovery_reason
  if (
    typeof event.data.uncertain_tool_call_id === "string" ||
    event.data.uncertain_tool_call_id === null
  )
    incoming.uncertainToolCallId = event.data.uncertain_tool_call_id
  return mergeRunInfo(previous, incoming)
}

export function allowsRunRetry(
  activity?: TurnRunActivity,
  latest?: LatestRun | null
): boolean {
  const runId =
    activity?.runInfo?.runId ?? activity?.summary?.run_id ?? latest?.run_id
  if (!runId) return true
  return !(
    (latest?.run_id === runId && latest.retry_allowed === false) ||
    activity?.runInfo?.retryAllowed === false ||
    (activity?.summary?.run_id === runId &&
      activity.summary.retry_allowed === false) ||
    activity?.events.some(
      (event) => event.runId === runId && event.data.retry_allowed === false
    )
  )
}
