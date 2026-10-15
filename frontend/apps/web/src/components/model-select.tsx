import type { ModelStatus } from "@/lib/app-types"
import { isModelSelectable, modelAvailabilityLabel } from "@/lib/model-choice"

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
  const hasSelection = models.some((model) => model.id === selectedModelId)
  const groups = new Map<string, ModelStatus[]>()
  for (const model of models) {
    const provider = model.provider_id || model.provider_name || "models"
    const group = groups.get(provider) ?? []
    group.push(model)
    groups.set(provider, group)
  }
  return (
    <>
      {!hasSelection ? (
        <option value={selectedModelId} disabled>
          {selectedModelId ? "Saved model unavailable" : "Choose a model"}
        </option>
      ) : null}
      {[...groups].map(([provider, models]) => (
        <optgroup
          key={provider}
          label={models[0].provider_name || models[0].provider_id || "Models"}
        >
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
        </optgroup>
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
        <ModelOptions models={models} selectedModelId={selectedModelId} />
      </select>
      <span className="sr-only" role="status">
        {saving ? "Saving chat model…" : ""}
      </span>
    </>
  )
}
