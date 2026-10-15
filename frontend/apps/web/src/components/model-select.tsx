import type { ModelStatus } from "@/lib/app-types"
import { isModelSelectable, modelAvailabilityLabel } from "@/lib/model-choice"

type ModelSelectProps = {
  models: ModelStatus[]
  selectedModelId: string
  disabled: boolean
  saving: boolean
  onChange: (modelId: string) => void
}

export function ModelSelect({
  models,
  selectedModelId,
  disabled,
  saving,
  onChange,
}: ModelSelectProps) {
  const hasSelection = models.some((model) => model.id === selectedModelId)
  return (
    <>
      <select
        className="composer-select composer-model-select"
        aria-label="Chat model"
        disabled={disabled || saving}
        aria-busy={saving}
        value={selectedModelId}
        title={saving ? "Saving chat model…" : "Model for this chat"}
        onChange={(event) => onChange(event.target.value)}
      >
        {!hasSelection ? (
          <option value={selectedModelId} disabled>
            {selectedModelId ? "Saved model unavailable" : "Choose a model"}
          </option>
        ) : null}
        {models.map((model) => (
          <option
            key={model.id}
            value={model.id}
            disabled={!isModelSelectable(model)}
          >
            {model.name}
            {!isModelSelectable(model)
              ? ` · ${modelAvailabilityLabel(model)}`
              : ""}
          </option>
        ))}
      </select>
      <span className="sr-only" role="status">
        {saving ? "Saving chat model…" : ""}
      </span>
    </>
  )
}
