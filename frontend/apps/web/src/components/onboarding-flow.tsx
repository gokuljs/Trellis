import { ArrowUpRight, Eye, EyeOff, KeyRound } from "lucide-react"
import { useEffect, useRef, useState, type ReactNode } from "react"

import { TrellisMark } from "@trellis/ui/icons/trellis-mark"
import { ApiError } from "@/lib/api"
import type {
  ModelId,
  ModelStatus,
  OnboardingStep,
  Profile,
  Settings,
} from "@/lib/app-types"

export type OnboardingValues = {
  modelId: ModelId
  apiKey: string
}

type OnboardingFlowProps = {
  accountControl?: ReactNode
  profile: Profile
  settings: Settings
  initialStep: OnboardingStep
  onAdvanceIntro: () => Promise<void>
  onSaveProfile: (displayName: string) => Promise<void>
  onComplete: (values: OnboardingValues) => Promise<void>
}

const STEP_NUMBER: Record<OnboardingStep, number> = {
  intro: 1,
  profile: 2,
  model: 3,
}

const onboardingImageUrl = new URL(
  "../../../../assets/image-1.jpg",
  import.meta.url
).href

function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "Trellis could not finish setup."
}

export function OnboardingFlow({
  accountControl,
  profile,
  settings,
  initialStep,
  onAdvanceIntro,
  onSaveProfile,
  onComplete,
}: OnboardingFlowProps) {
  const [step, setStep] = useState<OnboardingStep>(initialStep)
  const [direction, setDirection] = useState<"forward" | "back">("forward")
  const [displayName, setDisplayName] = useState(profile.display_name ?? "")
  const modelOptions: ModelStatus[] =
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
  const [modelId, setModelId] = useState<ModelId>(
    settings.selected_model_id ?? modelOptions[0]?.id ?? ""
  )
  const [apiKey, setApiKey] = useState("")
  const [showApiKey, setShowApiKey] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const modelRefs = useRef<Array<HTMLButtonElement | null>>([])

  const selectedModel = modelOptions.find((item) => item.id === modelId)

  useEffect(() => {
    headingRef.current?.focus()
  }, [step])

  const changeStep = (
    nextStep: OnboardingStep,
    nextDirection: "forward" | "back"
  ) => {
    setError(null)
    setDirection(nextDirection)
    setStep(nextStep)
  }

  const advanceIntro = async () => {
    setSubmitting(true)
    setError(null)
    try {
      await onAdvanceIntro()
      changeStep("profile", "forward")
    } catch (submitError) {
      setError(errorMessage(submitError))
    } finally {
      setSubmitting(false)
    }
  }

  const continueFromProfile = async () => {
    const trimmedName = displayName.trim()
    if (!trimmedName) {
      setError("Enter your name.")
      return
    }
    if (trimmedName.length > 100) {
      setError("Keep your name under 100 characters.")
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await onSaveProfile(trimmedName)
      changeStep("model", "forward")
    } catch (submitError) {
      setError(errorMessage(submitError))
    } finally {
      setSubmitting(false)
    }
  }

  const selectModel = (nextModel: ModelId) => {
    setModelId(nextModel)
    setApiKey("")
    setShowApiKey(false)
    setError(null)
  }

  const submit = async () => {
    if (!selectedModel) {
      setError("Choose a model.")
      return
    }
    const trimmedKey = apiKey.trim()
    if (
      selectedModel.requires_api_key &&
      !selectedModel.configured &&
      !trimmedKey
    ) {
      setError(`Add an API key for ${selectedModel.provider_name}.`)
      return
    }

    setError(null)
    setSubmitting(true)
    try {
      await onComplete({
        modelId: selectedModel.id,
        apiKey: selectedModel.configured ? "" : trimmedKey,
      })
    } catch (submitError) {
      setError(errorMessage(submitError))
    } finally {
      setSubmitting(false)
    }
  }

  const stepNumber = STEP_NUMBER[step]
  const stepLabel = `${String(stepNumber).padStart(2, "0")} / 03`

  return (
    <main className="onboarding-shell" data-theme="dark">
      <div className="onboarding-frame">
        <header className="onboarding-topbar">
          <div className="onboarding-brand" aria-label="Trellis">
            <TrellisMark size={18} />
            <span>Trellis</span>
          </div>
          <div className="onboarding-account-actions">
            <div className="onboarding-step" aria-live="polite">
              {stepLabel}
            </div>
            {accountControl}
          </div>
        </header>

        <section className="onboarding-content">
          <div className="onboarding-stage" aria-live="polite">
            <div
              key={step}
              className={`onboarding-page onboarding-page-${direction}`}
            >
              {step === "intro" ? (
                <section
                  className="onboarding-intro"
                  aria-labelledby="onboarding-intro-heading"
                >
                  <h1
                    id="onboarding-intro-heading"
                    ref={headingRef}
                    aria-label="Workspace setup"
                    tabIndex={-1}
                  >
                    <span>Workspace setup</span>
                  </h1>
                  <p className="onboarding-intro-copy">
                    Create your profile and connect a model.
                  </p>
                  <button
                    className="onboarding-primary-action"
                    type="button"
                    disabled={submitting}
                    onClick={() => void advanceIntro()}
                  >
                    <span>{submitting ? "Saving…" : "Continue"}</span>
                    <ArrowUpRight size={14} aria-hidden="true" />
                  </button>
                </section>
              ) : step === "profile" ? (
                <section
                  className="onboarding-form-page"
                  aria-labelledby="onboarding-profile-heading"
                >
                  <h1
                    id="onboarding-profile-heading"
                    ref={headingRef}
                    tabIndex={-1}
                  >
                    Your profile
                  </h1>
                  <form
                    className="onboarding-form"
                    onSubmit={(event) => {
                      event.preventDefault()
                      void continueFromProfile()
                    }}
                  >
                    <label className="onboarding-field">
                      <span>Name</span>
                      <input
                        aria-label="Name"
                        value={displayName}
                        maxLength={100}
                        autoComplete="name"
                        onChange={(event) => setDisplayName(event.target.value)}
                      />
                    </label>
                    <label className="onboarding-field">
                      <span>Email</span>
                      <input
                        aria-label="Email"
                        type="email"
                        readOnly
                        value={profile.email ?? ""}
                        autoComplete="email"
                      />
                    </label>
                    {error ? (
                      <div className="onboarding-error" role="alert">
                        {error}
                      </div>
                    ) : null}
                    <div className="onboarding-actions">
                      <button
                        className="onboarding-secondary-action"
                        type="button"
                        disabled={submitting}
                        onClick={() => changeStep("intro", "back")}
                      >
                        Back
                      </button>
                      <button
                        className="onboarding-primary-action"
                        type="submit"
                        disabled={submitting}
                      >
                        {submitting ? "Saving…" : "Continue"}
                      </button>
                    </div>
                  </form>
                </section>
              ) : (
                <section
                  className="onboarding-form-page"
                  aria-labelledby="onboarding-model-heading"
                >
                  <h1
                    id="onboarding-model-heading"
                    ref={headingRef}
                    tabIndex={-1}
                  >
                    Choose a model
                  </h1>
                  <form
                    className="onboarding-form"
                    onSubmit={(event) => {
                      event.preventDefault()
                      void submit()
                    }}
                  >
                    <div
                      className="onboarding-provider-options"
                      role="radiogroup"
                      aria-label="Available models"
                    >
                      {modelOptions.map((item, index) => (
                        <button
                          key={item.id}
                          ref={(element) => {
                            modelRefs.current[index] = element
                          }}
                          className={`onboarding-provider ${item.id === modelId ? "is-selected" : ""}`}
                          type="button"
                          role="radio"
                          aria-checked={item.id === modelId}
                          tabIndex={item.id === modelId ? 0 : -1}
                          onClick={() => selectModel(item.id)}
                          onKeyDown={(event) => {
                            let nextIndex: number | null = null
                            if (
                              event.key === "ArrowDown" ||
                              event.key === "ArrowRight"
                            ) {
                              nextIndex = (index + 1) % modelOptions.length
                            } else if (
                              event.key === "ArrowUp" ||
                              event.key === "ArrowLeft"
                            ) {
                              nextIndex =
                                (index - 1 + modelOptions.length) %
                                modelOptions.length
                            } else if (event.key === "Home") {
                              nextIndex = 0
                            } else if (event.key === "End") {
                              nextIndex = modelOptions.length - 1
                            }

                            if (nextIndex === null) return
                            event.preventDefault()
                            const nextModel = modelOptions[nextIndex]
                            if (!nextModel) return
                            selectModel(nextModel.id)
                            modelRefs.current[nextIndex]?.focus()
                          }}
                        >
                          <span>{item.provider_name}</span>
                          <span>{item.name}</span>
                          <span>
                            {item.configured
                              ? "Configured"
                              : item.requires_api_key
                                ? "API key required"
                                : "Ready to use"}
                          </span>
                        </button>
                      ))}
                    </div>

                    {selectedModel &&
                    selectedModel.requires_api_key &&
                    !selectedModel.configured ? (
                      <label className="onboarding-field">
                        <span>{selectedModel.provider_name} API key</span>
                        <span className="onboarding-key-input">
                          <KeyRound size={15} aria-hidden="true" />
                          <input
                            aria-label={`${selectedModel.provider_name} API key`}
                            type={showApiKey ? "text" : "password"}
                            value={apiKey}
                            autoComplete="new-password"
                            spellCheck={false}
                            onChange={(event) => setApiKey(event.target.value)}
                          />
                          <button
                            className="onboarding-key-toggle"
                            type="button"
                            aria-label={
                              showApiKey ? "Hide API key" : "Show API key"
                            }
                            onClick={() => setShowApiKey((visible) => !visible)}
                          >
                            {showApiKey ? (
                              <EyeOff size={15} aria-hidden="true" />
                            ) : (
                              <Eye size={15} aria-hidden="true" />
                            )}
                          </button>
                        </span>
                      </label>
                    ) : null}

                    {error ? (
                      <div className="onboarding-error" role="alert">
                        {error}
                      </div>
                    ) : null}
                    <div className="onboarding-actions">
                      <button
                        className="onboarding-secondary-action"
                        type="button"
                        disabled={submitting}
                        onClick={() => changeStep("profile", "back")}
                      >
                        Back
                      </button>
                      <button
                        className="onboarding-primary-action"
                        type="submit"
                        disabled={submitting}
                      >
                        {submitting ? "Starting…" : "Start Trellis"}
                      </button>
                    </div>
                  </form>
                </section>
              )}
            </div>
          </div>
        </section>

        <div className="onboarding-visual">
          <img
            className="onboarding-image"
            src={onboardingImageUrl}
            alt=""
            aria-hidden="true"
            width={5899}
            height={3938}
          />
        </div>
      </div>
    </main>
  )
}
