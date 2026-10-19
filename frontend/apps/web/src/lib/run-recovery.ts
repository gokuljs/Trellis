import type { Message } from "@/lib/app-types"

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
