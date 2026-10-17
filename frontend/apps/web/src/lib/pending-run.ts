import type { StartRunInput } from "@/lib/runtime-client"

export type PendingRun = Omit<StartRunInput, "content">

const prefix = "trellis.pending-run."

function storage(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function key(userId: string, sessionId: string) {
  return `${prefix}${encodeURIComponent(userId)}.${encodeURIComponent(sessionId)}`
}

export function readPendingRun(
  userId: string,
  sessionId: string,
  target: Storage | null = storage()
): PendingRun | null {
  try {
    const serialized = target?.getItem(key(userId, sessionId))
    if (!serialized) return null
    const value: unknown = JSON.parse(serialized)
    if (!value || typeof value !== "object") return null
    const pending = value as Record<string, unknown>
    if (
      pending.sessionId !== sessionId ||
      typeof pending.turnId !== "string" ||
      !pending.turnId ||
      typeof pending.clientRequestId !== "string" ||
      !pending.clientRequestId ||
      (pending.modelId !== undefined && typeof pending.modelId !== "string") ||
      (pending.budgetPreset !== undefined &&
        pending.budgetPreset !== "conservative" &&
        pending.budgetPreset !== "longer")
    )
      return null
    return {
      sessionId,
      turnId: pending.turnId,
      clientRequestId: pending.clientRequestId,
      ...(typeof pending.modelId === "string"
        ? { modelId: pending.modelId }
        : {}),
      ...(pending.budgetPreset ? { budgetPreset: pending.budgetPreset } : {}),
    }
  } catch {
    return null
  }
}

export function rememberPendingRun(
  userId: string,
  input: StartRunInput,
  target: Storage | null = storage()
): StartRunInput {
  const previous = readPendingRun(userId, input.sessionId, target)
  const clientRequestId =
    previous?.turnId === input.turnId &&
    previous.modelId === input.modelId &&
    previous.budgetPreset === input.budgetPreset
      ? previous.clientRequestId
      : input.clientRequestId
  const pending: PendingRun = {
    sessionId: input.sessionId,
    turnId: input.turnId,
    clientRequestId,
    ...(input.modelId !== undefined ? { modelId: input.modelId } : {}),
    ...(input.budgetPreset !== undefined
      ? { budgetPreset: input.budgetPreset }
      : {}),
  }
  try {
    target?.setItem(key(userId, input.sessionId), JSON.stringify(pending))
  } catch {
    // Storage can be disabled by browser privacy settings.
  }
  return { ...input, clientRequestId }
}

export function forgetPendingRun(
  userId: string,
  sessionId: string,
  clientRequestId?: string,
  target: Storage | null = storage()
) {
  if (
    clientRequestId !== undefined &&
    readPendingRun(userId, sessionId, target)?.clientRequestId !==
      clientRequestId
  )
    return
  try {
    target?.removeItem(key(userId, sessionId))
  } catch {
    // Storage can be disabled by browser privacy settings.
  }
}

export function clearPendingRuns(target: Storage | null = storage()) {
  try {
    for (let index = (target?.length ?? 0) - 1; index >= 0; index -= 1) {
      const name = target?.key(index)
      if (name?.startsWith(prefix)) target?.removeItem(name)
    }
  } catch {
    // Storage can be disabled by browser privacy settings.
  }
}
