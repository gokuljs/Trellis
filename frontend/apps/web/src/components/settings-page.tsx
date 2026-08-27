import { Check, Eye, EyeOff, KeyRound, Sparkles, Trash2 } from "lucide-react"
import { useState } from "react"

import { ApiError, api } from "@/lib/api"
import type { Profile, ProviderId, Settings } from "@/lib/app-types"
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
  const [email, setEmail] = useState(profile.email ?? "")
  const [apiKey, setApiKey] = useState("")
  const [showApiKey, setShowApiKey] = useState(false)
  const [saving, setSaving] = useState<string | null>(null)
  const selectedProvider =
    settings.providers.find((item) => item.id === settings.selected_provider) ??
    settings.providers[0]

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

  const selectProvider = (provider: ProviderId) => {
    const nextProvider = settings.providers.find((item) => item.id === provider)
    if (!nextProvider) return
    setApiKey("")
    setShowApiKey(false)
    void runUpdate(
      "provider",
      {
        kind: "info",
        title: `${nextProvider.name} selected`,
        description: `${nextProvider.model} will power new messages.`,
      },
      async () => {
        onSettingsChange(await api.selectProvider(provider))
      }
    )
  }

  if (!selectedProvider) return null

  return (
    <div className="settings-page">
      <h1 className="sr-only">Settings</h1>
      <header className="settings-header">
        <div className="utility-kicker">SETTINGS</div>
        <p>
          Set up this local installation. Your profile and sessions stay on this
          device.
        </p>
      </header>

      <div className="settings-sections">
        <section className="settings-section" aria-labelledby="profile-heading">
          <div className="settings-section-copy">
            <div className="settings-section-label">LOCAL PROFILE</div>
            <h2 id="profile-heading">Your installation</h2>
            <p>
              Optional details make the workspace feel familiar. The
              installation ID identifies this local user.
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
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@example.com"
                />
              </span>
            </label>
            <label className="settings-field settings-field-wide">
              <span className="settings-field-label">Installation ID</span>
              <span className="settings-input-wrap readonly">
                <input
                  aria-label="Installation ID"
                  value={profile.id}
                  disabled
                  readOnly
                />
              </span>
              <span className="settings-field-help">
                Stable until the Trellis data directory is removed.
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
                    description: "Your details stay on this device.",
                  },
                  async () => {
                    onProfileChange(
                      await api.updateProfile({
                        display_name: displayName.trim() || null,
                        email: email.trim() || null,
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
            <div className="settings-section-label">MODEL PROVIDER</div>
            <h2 id="provider-heading">Choose your model</h2>
            <p>
              The selected provider powers every new turn. Your choice is stored
              locally.
            </p>
          </div>

          <div
            className="provider-options"
            role="radiogroup"
            aria-label="Model provider"
          >
            {settings.providers.map((item) => {
              const isSelected = item.id === settings.selected_provider
              return (
                <button
                  className={`provider-option ${isSelected ? "is-selected" : ""}`}
                  key={item.id}
                  type="button"
                  role="radio"
                  aria-checked={isSelected}
                  disabled={saving !== null}
                  onClick={() => selectProvider(item.id)}
                >
                  <span className="provider-icon">
                    <Sparkles size={16} strokeWidth={1.7} aria-hidden="true" />
                  </span>
                  <span className="provider-details">
                    <span className="provider-name">{item.name}</span>
                    <span className="provider-description">
                      {item.configured
                        ? `Key configured ${item.key_hint ?? ""}`
                        : "API key required"}
                    </span>
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
            <h2 id="api-key-heading">Connect {selectedProvider.name}</h2>
            <p>
              Keys are stored separately from chat history in the private local
              secrets file.
            </p>
          </div>

          <div className="settings-key-panel">
            <label className="settings-field">
              <span className="settings-field-label">
                {selectedProvider.name} API key
              </span>
              <span className="settings-input-wrap">
                <KeyRound size={15} strokeWidth={1.7} aria-hidden="true" />
                <input
                  type={showApiKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={
                    selectedProvider.id === "openai" ? "sk-…" : "sk-ant-…"
                  }
                  autoComplete="new-password"
                  name={`${selectedProvider.id}-api-key`}
                  spellCheck={false}
                  aria-label={`${selectedProvider.name} API key`}
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
                {selectedProvider.configured
                  ? `Configured · ${selectedProvider.key_hint ?? "saved"}`
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
                      title: `${selectedProvider.name} key saved`,
                      description: "Stored in your private local secrets file.",
                    },
                    async () => {
                      onSettingsChange(
                        await api.saveApiKey(selectedProvider.id, apiKey.trim())
                      )
                      setApiKey("")
                      setShowApiKey(false)
                    }
                  )
                }}
              >
                {saving === "key"
                  ? "Saving…"
                  : `Save ${selectedProvider.name} key`}
              </button>
              {selectedProvider.configured ? (
                <button
                  className="settings-remove"
                  type="button"
                  disabled={saving !== null}
                  onClick={() => {
                    void runUpdate(
                      "remove",
                      {
                        kind: "info",
                        title: `${selectedProvider.name} key removed`,
                        description: "New messages will require another key.",
                      },
                      async () => {
                        onSettingsChange(
                          await api.removeApiKey(selectedProvider.id)
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
      </div>
    </div>
  )
}
