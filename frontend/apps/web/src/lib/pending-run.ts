import type { RunMode } from "@/lib/app-types"

export type PendingRun = {
  userId: string
  sessionId: string
  turnId: string
  clientRequestId: string
  runId: string | null
  lastSequence: number
  runMode?: RunMode
}

const prefix = "trellis:pending-run:"
const key = (userId: string, sessionId: string) =>
  `${prefix}${encodeURIComponent(userId)}:${encodeURIComponent(sessionId)}`

export function readPendingRun(
  userId: string,
  sessionId: string
): PendingRun | null {
  try {
    const raw = localStorage.getItem(key(userId, sessionId))
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<PendingRun>
    if (
      value.userId !== userId ||
      value.sessionId !== sessionId ||
      typeof value.turnId !== "string" ||
      typeof value.clientRequestId !== "string" ||
      !(value.runId === null || typeof value.runId === "string") ||
      typeof value.lastSequence !== "number" ||
      !Number.isSafeInteger(value.lastSequence) ||
      value.lastSequence < 0 ||
      (value.runMode !== undefined &&
        value.runMode !== "chat" &&
        value.runMode !== "research")
    )
      return null
    return value as PendingRun
  } catch {
    return null
  }
}

export function savePendingRun(pending: PendingRun): void {
  try {
    localStorage.setItem(
      key(pending.userId, pending.sessionId),
      JSON.stringify({
        userId: pending.userId,
        sessionId: pending.sessionId,
        turnId: pending.turnId,
        clientRequestId: pending.clientRequestId,
        runId: pending.runId,
        lastSequence: pending.lastSequence,
        runMode: pending.runMode,
      })
    )
  } catch {
    // Server discovery remains available when browser storage is unavailable.
  }
}

export function clearPendingRun(
  userId: string,
  sessionId: string,
  clientRequestId?: string
): void {
  try {
    if (
      clientRequestId &&
      readPendingRun(userId, sessionId)?.clientRequestId !== clientRequestId
    )
      return
    localStorage.removeItem(key(userId, sessionId))
  } catch {
    // Browser storage can be disabled independently of the signed-in session.
  }
}

export function clearUserPendingRuns(userId: string): void {
  try {
    const userPrefix = `${prefix}${encodeURIComponent(userId)}:`
    for (const entry of Object.keys(localStorage)) {
      if (entry.startsWith(userPrefix)) localStorage.removeItem(entry)
    }
  } catch {
    // Cleanup must not prevent signing out.
  }
}
