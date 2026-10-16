import type { ModelStatus } from "@/lib/app-types"
import { isModelSelectable } from "@/lib/model-choice"

type ModelSelectProps = {
  models: ModelStatus[]
  selectedModelId: string
  disabled: boolean
  saving: boolean
  onChange: (modelId: string) => void
}

export function ModelOptions({
  models,
  selectedModelId,
}: Pick<ModelSelectProps, "models" | "selectedModelId">) {
  const visibleModels = models.filter(isModelSelectable)
  const selectedModel = models.find((model) => model.id === selectedModelId)
  const hasSelection = visibleModels.some(
    (model) => model.id === selectedModelId
  )
  return (
    <>
      {!hasSelection ? (
        <option value={selectedModelId} disabled>
          {selectedModelId
            ? `${selectedModel?.name ?? selectedModelId} (unavailable)`
            : visibleModels.length
              ? "Choose a model"
              : "No models available"}
        </option>
      ) : null}
      {visibleModels.map((model) => (
        <option key={model.id} value={model.id}>
          {model.name}
        </option>
      ))}
    </>
  )
}

export function ModelSelect({
  models,
  selectedModelId,
  disabled,
  saving,
  onChange,
}: ModelSelectProps) {
  const hasModels = models.some(isModelSelectable)
  return (
    <>
      <select
        className="composer-select composer-model-select"
        aria-label="Chat model"
        disabled={disabled || saving || !hasModels}
        aria-busy={saving}
        value={selectedModelId}
        title={saving ? "Saving chat model…" : "Model for this chat"}
        onChange={(event) => onChange(event.target.value)}
      >
        <ModelOptions models={models} selectedModelId={selectedModelId} />
      </select>
      <span className="sr-only" role="status">
        {saving ? "Saving chat model…" : ""}
      </span>
    </>
  )
}
