import type { ModelStatus } from "@/lib/app-types"

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
  return (
    <>
      {!hasSelection ? (
        <option value={selectedModelId} disabled>
          {selectedModelId || "Choose a model"}
        </option>
      ) : null}
      {models.map((model) => (
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
