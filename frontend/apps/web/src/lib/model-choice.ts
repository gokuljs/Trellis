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
  if (model.metadata_status === "pending") return "Pending"
  return isModelSelectable(model) ? "Ready to use" : "Unavailable"
}

export function selectedChatModelId(
  session: Pick<Session, "model_id"> | null,
  settings: Pick<Settings, "selected_model_id" | "models"> | null,
  draftModelId: string | null = null
): string {
  return (
    session?.model_id ??
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
