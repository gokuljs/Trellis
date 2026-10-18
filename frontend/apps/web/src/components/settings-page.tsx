import { useEffect, useRef, useState } from "react"

import { ModelOptions } from "@/components/model-select"
import { ApiError, api } from "@/lib/api"
import type {
  ModelStatus,
  OnboardingState,
  Profile,
  Settings,
} from "@/lib/app-types"
import { notifications } from "@/lib/notifications"
import { isModelSelectable } from "@/lib/model-choice"
import { reportFailure } from "@/lib/telemetry"
import "./profile-summary.css"

type SettingsPageProps = {
  profile: Profile
  settings: Settings
  onboarding?: OnboardingState
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
  onboarding,
  onProfileChange,
  onSettingsChange,
}: SettingsPageProps) {
  const [displayName, setDisplayName] = useState(profile.display_name ?? "")
  const [saving, setSaving] = useState<string | null>(null)
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
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
  const hasModels = models.some(isModelSelectable)
  const setupStatus = onboarding
    ? onboarding.completed
      ? "Complete"
      : "In progress"
    : "Setup status unavailable"

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
      if (mounted.current) reportFailure(updateError, "settings.save")
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
    if (!isModelSelectable(nextModel)) return
    void runUpdate(
      "model",
      {
        kind: "info",
        title: `${nextModel.name} selected`,
        description: `${nextModel.provider_name} will power new chats.`,
      },
      async () => {
        onSettingsChange(await api.selectModel(modelId))
      }
    )
  }

  return (
    <div className="settings-page">
      <h1 className="sr-only">Settings</h1>
      <header className="settings-header">
        <div className="utility-kicker">SETTINGS</div>
        <p>Manage your account, model, and run preferences.</p>
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
            <section
              className="profile-setup-summary"
              aria-labelledby="saved-setup-heading"
            >
              <h3 id="saved-setup-heading">Saved setup</h3>
              <dl>
                <div>
                  <dt>Name</dt>
                  <dd>{profile.display_name?.trim() || "Not set"}</dd>
                </div>
                <div>
                  <dt>Email</dt>
                  <dd>{profile.email || "Not available"}</dd>
                </div>
                <div>
                  <dt>Onboarding</dt>
                  <dd>{setupStatus}</dd>
                </div>
                <div>
                  <dt>Default model</dt>
                  <dd>{selectedModel?.name ?? selectedModelId}</dd>
                </div>
              </dl>
            </section>
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
            <h2 id="provider-heading">Default model</h2>
            <p>
              New chats use this model. Existing chats keep their own choice.
            </p>
          </div>

          <div className="settings-form-grid">
            <label className="settings-field">
              <span className="settings-field-label">Model for new chats</span>
              <span className="settings-input-wrap">
                <select
                  aria-label="Default model for new chats"
                  value={selectedModelId}
                  disabled={saving !== null || !hasModels}
                  aria-busy={saving === "model"}
                  onChange={(event) => selectModel(event.target.value)}
                >
                  <ModelOptions
                    models={models}
                    selectedModelId={selectedModelId}
                  />
                </select>
              </span>
              {saving === "model" ? (
                <span className="settings-field-help" role="status">
                  Saving default model…
                </span>
              ) : null}
            </label>
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
      </div>
    </div>
  )
}
