import { Check, Eye, EyeOff, KeyRound, Sparkles, Trash2 } from "lucide-react"
import { useState } from "react"

import { ApiError, api } from "@/lib/api"
import type { ModelStatus, Profile, Settings } from "@/lib/app-types"
import { notifications } from "@/lib/notifications"

type SettingsPageProps = {
  profile: Profile
  settings: Settings
  onProfileChange: (profile: Profile) => void
  onSettingsChange: (settings: Settings) => void
}

function errorMessage(error: unknown) {
  return error instanceof ApiError
    ? error.message
    : "Trellis could not save that change."
}

export function SettingsPage({
  profile,
  settings,
  onProfileChange,
  onSettingsChange,
}: SettingsPageProps) {
  const [displayName, setDisplayName] = useState(profile.display_name ?? "")
  const [apiKey, setApiKey] = useState("")
  const [showApiKey, setShowApiKey] = useState(false)
  const [saving, setSaving] = useState<string | null>(null)
  const models: ModelStatus[] =
    settings.models ??
    settings.providers.map((provider) => ({
      id: provider.id,
      provider_id: provider.id,
      provider_name: provider.name,
      adapter_kind: provider.id,
      upstream_model_id: provider.model,
      name: provider.model,
      requires_api_key: true,
      supports_streaming: true,
      supports_tools: false,
      configured: provider.configured,
      key_hint: provider.key_hint,
    }))
  const selectedModelId =
    settings.selected_model_id ?? settings.selected_provider
  const selectedModel = models.find((model) => model.id === selectedModelId)
  const selectedProviderStatus = settings.providers.find(
    (provider) => provider.id === selectedModel?.provider_id
  )
  const credentialConfigured =
    selectedProviderStatus?.configured ?? selectedModel?.configured ?? false
  const credentialHint =
    selectedProviderStatus?.key_hint ?? selectedModel?.key_hint

  const runUpdate = async (
    label: string,
    success: {
      kind?: "success" | "info"
      title: string
      description: string
    },
    update: () => Promise<void>
  ) => {
    setSaving(label)
    try {
      await update()
      notifications[success.kind ?? "success"](success)
    } catch (updateError) {
      notifications.error({
        title: "Change not saved",
        description: errorMessage(updateError),
      })
    } finally {
      setSaving(null)
    }
  }

  const selectModel = (modelId: string) => {
    const nextModel = models.find((item) => item.id === modelId)
    if (!nextModel) return
    setApiKey("")
    setShowApiKey(false)
    void runUpdate(
      "model",
      {
        kind: "info",
        title: `${nextModel.name} selected`,
        description: `${nextModel.provider_name} will power new messages.`,
      },
      async () => {
        onSettingsChange(await api.selectModel(modelId))
      }
    )
  }

  if (!selectedModel) return null

  return (
    <div className="settings-page">
      <h1 className="sr-only">Settings</h1>
      <header className="settings-header">
        <div className="utility-kicker">SETTINGS</div>
        <p>Manage your account, model, and local provider keys.</p>
      </header>

      <div className="settings-sections">
        <section className="settings-section" aria-labelledby="profile-heading">
          <div className="settings-section-copy">
            <div className="settings-section-label">ACCOUNT</div>
            <h2 id="profile-heading">Your profile</h2>
            <p>
              Your sign-in email is managed by your account. You can change how
              Trellis addresses you here.
            </p>
          </div>

          <div className="settings-form-grid">
            <label className="settings-field">
              <span className="settings-field-label">Display name</span>
              <span className="settings-input-wrap">
                <input
                  aria-label="Display name"
                  value={displayName}
                  maxLength={100}
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder="How should Trellis address you?"
                />
              </span>
            </label>
            <label className="settings-field">
              <span className="settings-field-label">Email</span>
              <span className="settings-input-wrap">
                <input
                  aria-label="Email"
                  type="email"
                  value={profile.email ?? ""}
                  readOnly
                />
              </span>
            </label>
            <label className="settings-field settings-field-wide">
              <span className="settings-field-label">Account ID</span>
              <span className="settings-input-wrap readonly">
                <input
                  aria-label="Account ID"
                  value={profile.id}
                  disabled
                  readOnly
                />
              </span>
              <span className="settings-field-help">
                This ID identifies your account across devices.
              </span>
            </label>
            <button
              className="settings-save"
              type="button"
              disabled={saving !== null}
              onClick={() => {
                void runUpdate(
                  "profile",
                  {
                    title: "Profile saved",
                    description: "Your display name has been updated.",
                  },
                  async () => {
                    onProfileChange(
                      await api.updateProfile({
                        display_name: displayName.trim() || null,
                      })
                    )
                  }
                )
              }}
            >
              {saving === "profile" ? "Saving…" : "Save profile"}
            </button>
          </div>
        </section>

        <section
          className="settings-section"
          aria-labelledby="provider-heading"
        >
          <div className="settings-section-copy">
            <div className="settings-section-label">MODEL</div>
            <h2 id="provider-heading">Choose your model</h2>
            <p>Choose which available model powers new turns.</p>
          </div>

          <div
            className="provider-options"
            role="radiogroup"
            aria-label="Available models"
          >
            {models.map((item) => {
              const isSelected = item.id === selectedModelId
              return (
                <button
                  className={`provider-option ${isSelected ? "is-selected" : ""}`}
                  key={item.id}
                  type="button"
                  role="radio"
                  aria-checked={isSelected}
                  disabled={saving !== null}
                  onClick={() => selectModel(item.id)}
                >
                  <span className="provider-icon">
                    <Sparkles size={16} strokeWidth={1.7} aria-hidden="true" />
                  </span>
                  <span className="provider-details">
                    <span className="provider-name">{item.provider_name}</span>
                    <span className="provider-description">
                      {item.configured
                        ? `Key configured ${item.key_hint ?? ""}`
                        : item.requires_api_key
                          ? "API key required"
                          : "Ready to use"}
                    </span>
                  </span>
                  <span className="provider-model">{item.name}</span>
                  <span className="provider-check" aria-hidden="true">
                    {isSelected ? <Check size={14} strokeWidth={2} /> : null}
                  </span>
                </button>
              )
            })}
          </div>
        </section>

        <section
          className="settings-section"
          aria-labelledby="default-budget-heading"
        >
          <div className="settings-section-copy">
            <div className="settings-section-label">RUN BUDGET</div>
            <h2 id="default-budget-heading">Default run budget</h2>
            <p>
              New runs use this limit unless you choose another in the composer.
            </p>
          </div>
          <label className="settings-field">
            <span className="settings-field-label">Default run budget</span>
            <span className="settings-input-wrap">
              <select
                aria-label="Default run budget"
                value={settings.default_budget_preset ?? "conservative"}
                disabled={saving !== null}
                onChange={(event) => {
                  const preset = event.target.value as "conservative" | "longer"
                  void runUpdate(
                    "budget",
                    {
                      kind: "info",
                      title: "Run budget updated",
                      description: "New runs use your selected default.",
                    },
                    async () => {
                      onSettingsChange(await api.selectDefaultBudget(preset))
                    }
                  )
                }}
              >
                <option value="conservative">Standard</option>
                <option value="longer">Extended</option>
              </select>
            </span>
          </label>
        </section>

        {selectedModel.requires_api_key ? (
          <section
            className="settings-section"
            aria-labelledby="api-key-heading"
          >
            <div className="settings-section-copy">
              <div className="settings-section-label">API KEY</div>
              <h2 id="api-key-heading">
                Connect {selectedModel.provider_name}
              </h2>
              <p>
                Keys are stored separately from chat history in the private
                local secrets file.
              </p>
            </div>

            <div className="settings-key-panel">
              <label className="settings-field">
                <span className="settings-field-label">
                  {selectedModel.provider_name} API key
                </span>
                <span className="settings-input-wrap">
                  <KeyRound size={15} strokeWidth={1.7} aria-hidden="true" />
                  <input
                    type={showApiKey ? "text" : "password"}
                    value={apiKey}
                    onChange={(event) => setApiKey(event.target.value)}
                    placeholder="Enter API key"
                    autoComplete="new-password"
                    name={`${selectedModel.provider_id}-api-key`}
                    spellCheck={false}
                    aria-label={`${selectedModel.provider_name} API key`}
                  />
                  <button
                    className="settings-input-action"
                    type="button"
                    aria-label={showApiKey ? "Hide API key" : "Show API key"}
                    onClick={() => setShowApiKey((visible) => !visible)}
                  >
                    {showApiKey ? (
                      <EyeOff size={15} aria-hidden="true" />
                    ) : (
                      <Eye size={15} aria-hidden="true" />
                    )}
                  </button>
                </span>
                <span className="settings-field-help">
                  {credentialConfigured
                    ? `Configured · ${credentialHint ?? "saved"}`
                    : "Not configured. The key is write-only and will not be shown again."}
                </span>
              </label>
              <div className="settings-actions">
                <button
                  className="settings-save"
                  type="button"
                  disabled={!apiKey.trim() || saving !== null}
                  onClick={() => {
                    void runUpdate(
                      "key",
                      {
                        title: `${selectedModel.provider_name} key saved`,
                        description:
                          "Stored in your private local secrets file.",
                      },
                      async () => {
                        onSettingsChange(
                          await api.saveApiKey(
                            selectedModel.provider_id,
                            apiKey.trim()
                          )
                        )
                        setApiKey("")
                        setShowApiKey(false)
                      }
                    )
                  }}
                >
                  {saving === "key"
                    ? "Saving…"
                    : `Save ${selectedModel.provider_name} key`}
                </button>
                {credentialConfigured ? (
                  <button
                    className="settings-remove"
                    type="button"
                    disabled={saving !== null}
                    onClick={() => {
                      void runUpdate(
                        "remove",
                        {
                          kind: "info",
                          title: `${selectedModel.provider_name} key removed`,
                          description: "New messages will require another key.",
                        },
                        async () => {
                          onSettingsChange(
                            await api.removeApiKey(selectedModel.provider_id)
                          )
                          setApiKey("")
                        }
                      )
                    }}
                  >
                    <Trash2 size={13} aria-hidden="true" /> Remove key
                  </button>
                ) : null}
              </div>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  )
}
