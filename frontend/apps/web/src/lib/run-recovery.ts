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
  terminalOutcome: TerminalRunOutcome | null
): "reconnect" | "reload" | "retry" {
  if (terminalOutcome === "completed") return "reload"
  if (
    terminalOutcome === null &&
    ["connection_lost", "runtime_timeout"].includes(code ?? "")
  )
    return "reconnect"
  return "retry"
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
