import type { ModelStatus, Session, Settings } from "@/lib/app-types"
import { ownsRun, type RunOwner } from "@/lib/run-recovery"

export function isModelSelectable(model: ModelStatus | undefined): boolean {
  if (
    !model ||
    model.enabled === false ||
    model.metadata_status === "pending" ||
    model.availability === "unavailable" ||
    (model.requires_api_key && !model.configured)
  )
    return false
  return model.selectable ?? (model.configured || !model.requires_api_key)
}

export function modelAvailabilityLabel(model: ModelStatus): string {
  if (isModelSelectable(model)) return "Ready to use"
  if (model.requires_api_key && !model.configured) return "API key missing"
  if (model.metadata_status === "pending") return "Metadata pending review"
  if (
    model.availability === "unavailable" ||
    model.pending_reasons?.includes("provider_unavailable")
  )
    return "Not available from provider"
  if (model.supports_streaming === false) return "Streaming not supported"
  if (model.supports_tools === false) return "Tools not supported"
  if (
    model.availability === "unknown" ||
    model.pending_reasons?.includes("availability_unverified")
  )
    return "Availability not checked"
  if (model.enabled === false) return "Disabled"
  return "Unavailable"
}

export function selectedChatModelId(
  session: (Pick<Session, "model_id"> & Partial<Pick<Session, "id">>) | null,
  settings: Pick<Settings, "selected_model_id" | "models"> | null,
  draftModelId: string | null = null,
  cachedSession: Pick<Session, "id" | "model_id"> | null = null
): string {
  const remembered =
    cachedSession && cachedSession.id === session?.id ? cachedSession : session
  return (
    remembered?.model_id ??
    (!session ? draftModelId : null) ??
    settings?.selected_model_id ??
    settings?.models?.find(isModelSelectable)?.id ??
    ""
  )
}

export function nextSelectableModelIndex(
  models: ModelStatus[],
  currentIndex: number,
  key: string
): number | null {
  const selectable = models.flatMap((model, index) =>
    isModelSelectable(model) ? [index] : []
  )
  if (selectable.length === 0) return null
  if (key === "Home") return selectable[0]
  if (key === "End") return selectable.at(-1)!
  const direction =
    key === "ArrowDown" || key === "ArrowRight"
      ? 1
      : key === "ArrowUp" || key === "ArrowLeft"
        ? -1
        : null
  if (direction === null) return null
  const position = selectable.indexOf(currentIndex)
  if (position < 0) return direction === 1 ? selectable[0] : selectable.at(-1)!
  return selectable[
    (position + direction + selectable.length) % selectable.length
  ]
}

export type ModelSelectionOwner = RunOwner & { requestSequence: number }

export function ownsModelSelection(
  owner: ModelSelectionOwner,
  current: ModelSelectionOwner
): boolean {
  return (
    ownsRun(owner, current) && owner.requestSequence === current.requestSequence
  )
}

export async function ensureSessionModel<
  T extends Pick<Session, "id" | "model_id">,
>(
  session: T,
  modelId: string,
  save: (sessionId: string, modelId: string) => Promise<T | null>
): Promise<T | null> {
  return session.model_id === modelId ? session : save(session.id, modelId)
}
