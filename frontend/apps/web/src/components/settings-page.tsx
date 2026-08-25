import { Check, Eye, EyeOff, KeyRound, Sparkles } from "lucide-react"
import { useState } from "react"

type ModelProvider = "OpenAI" | "Anthropic"

const providers: Array<{
  id: ModelProvider
  description: string
  model: string
}> = [
  { id: "OpenAI", description: "GPT-5.5 and other OpenAI models", model: "GPT-5.5" },
  { id: "Anthropic", description: "Claude and other Anthropic models", model: "Claude Sonnet" },
]

export function SettingsPage() {
  const [provider, setProvider] = useState<ModelProvider>("OpenAI")
  const [apiKeys, setApiKeys] = useState<Record<ModelProvider, string>>({ OpenAI: "", Anthropic: "" })
  const [showApiKey, setShowApiKey] = useState(false)
  const selectedProvider = providers.find((item) => item.id === provider) ?? providers[0]

  return (
    <div className="settings-page">
      <h1 className="sr-only">Settings</h1>
      <header className="settings-header">
        <div className="utility-kicker">SETTINGS</div>
        <p>Choose the model provider you want to work with and add its API key.</p>
      </header>

      <div className="settings-sections">
        <section className="settings-section" aria-labelledby="provider-heading">
          <div className="settings-section-copy">
            <div className="settings-section-label">MODEL PROVIDER</div>
            <h2 id="provider-heading">Choose your models</h2>
            <p>Pick the provider that should power your sessions.</p>
          </div>

          <div className="provider-options" role="radiogroup" aria-label="Model provider">
            {providers.map((item) => {
              const isSelected = item.id === provider

              return (
                <button
                  className={`provider-option ${isSelected ? "is-selected" : ""}`}
                  key={item.id}
                  type="button"
                  role="radio"
                  aria-checked={isSelected}
                  onClick={() => setProvider(item.id)}
                >
                  <span className="provider-icon"><Sparkles size={16} strokeWidth={1.7} aria-hidden="true" /></span>
                  <span className="provider-details">
                    <span className="provider-name">{item.id}</span>
                    <span className="provider-description">{item.description}</span>
                  </span>
                  <span className="provider-model">{item.model}</span>
                  <span className="provider-check" aria-hidden="true">
                    {isSelected ? <Check size={14} strokeWidth={2} /> : null}
                  </span>
                </button>
              )
            })}
          </div>
        </section>

        <section className="settings-section" aria-labelledby="api-key-heading">
          <div className="settings-section-copy">
            <div className="settings-section-label">API KEY</div>
            <h2 id="api-key-heading">Connect {selectedProvider.id}</h2>
            <p>Use a key from {selectedProvider.id} to enable its models in Trellis.</p>
          </div>

          <label className="settings-field">
            <span className="settings-field-label">{selectedProvider.id} API key</span>
            <span className="settings-input-wrap">
              <KeyRound size={15} strokeWidth={1.7} aria-hidden="true" />
              <input
                type={showApiKey ? "text" : "password"}
                value={apiKeys[selectedProvider.id]}
                onChange={(event) => setApiKeys((current) => ({ ...current, [selectedProvider.id]: event.target.value }))}
                placeholder={selectedProvider.id === "OpenAI" ? "sk-…" : "sk-ant-…"}
                autoComplete="off"
                name={`${selectedProvider.id.toLowerCase()}-api-key`}
                spellCheck={false}
                aria-label={`${selectedProvider.id} API key`}
              />
              <button
                className="settings-input-action"
                type="button"
                aria-label={showApiKey ? "Hide API key" : "Show API key"}
                onClick={() => setShowApiKey((visible) => !visible)}
              >
                {showApiKey ? <EyeOff size={15} strokeWidth={1.7} aria-hidden="true" /> : <Eye size={15} strokeWidth={1.7} aria-hidden="true" />}
              </button>
            </span>
            <span className="settings-field-help">Your key is only held in this form for now.</span>
          </label>
        </section>
      </div>
    </div>
  )
}
