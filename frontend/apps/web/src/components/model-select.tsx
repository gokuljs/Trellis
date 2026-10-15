import type { ModelStatus } from "@/lib/app-types"
import { modelProviderId, modelProviders } from "@/lib/model-choice"

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
  providerId,
}: Pick<ModelSelectProps, "models" | "selectedModelId"> & {
  providerId?: string | null
}) {
  const provider =
    providerId === undefined
      ? modelProviderId(
          models.find((model) => model.id === selectedModelId) ?? {
            id: selectedModelId,
          }
        )
      : providerId
  const visibleModels = models.filter(
    (model) => modelProviderId(model) === provider
  )
  const hasSelection = visibleModels.some(
    (model) => model.id === selectedModelId
  )
  return (
    <>
      {!hasSelection ? (
        <option value={selectedModelId} disabled>
          {selectedModelId || "Choose a model"}
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
  const providers = modelProviders(models)
  const providerId = modelProviderId(
    models.find((model) => model.id === selectedModelId) ?? {
      id: selectedModelId,
    }
  )
  const hasProvider = providers.some((provider) => provider.id === providerId)
  return (
    <>
      {providers.length > 1 || (providers.length > 0 && !hasProvider) ? (
        <select
          className="composer-select composer-provider-select"
          aria-label="Chat provider"
          disabled={disabled || saving}
          aria-busy={saving}
          value={providerId ?? ""}
          title={saving ? "Saving chat model…" : "Provider for this chat"}
          onChange={(event) => {
            const model = models.find(
              (item) => modelProviderId(item) === event.target.value
            )
            if (model) onChange(model.id)
          }}
        >
          {!hasProvider ? (
            <option value={providerId ?? ""} disabled>
              {providerId || "Choose a provider"}
            </option>
          ) : null}
          {providers.map((provider) => (
            <option key={provider.id} value={provider.id}>
              {provider.name}
            </option>
          ))}
        </select>
      ) : null}
      <select
        className="composer-select composer-model-select"
        aria-label="Chat model"
        disabled={disabled || saving}
        aria-busy={saving}
        value={selectedModelId}
        title={saving ? "Saving chat model…" : "Model for this chat"}
        onChange={(event) => onChange(event.target.value)}
      >
        <ModelOptions
          models={models}
          selectedModelId={selectedModelId}
          providerId={providerId}
        />
      </select>
      <span className="sr-only" role="status">
        {saving ? "Saving chat model…" : ""}
      </span>
    </>
  )
}
