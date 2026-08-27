import { ArrowUpRight, Eye, EyeOff, KeyRound } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { TrellisMark } from "@/components/trellis-mark"
import { ApiError } from "@/lib/api"
import type { Profile, ProviderId, Settings } from "@/lib/app-types"

export type OnboardingValues = {
  displayName: string
  email: string
  provider: ProviderId
  apiKey: string
}

type OnboardingFlowProps = {
  profile: Profile
  settings: Settings
  onComplete: (values: OnboardingValues) => Promise<void>
}

type OnboardingStep = "intro" | "profile" | "model"

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

function isValidEmail(value: string) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
}

export function OnboardingFlow({
  profile,
  settings,
  onComplete,
}: OnboardingFlowProps) {
  const [step, setStep] = useState<OnboardingStep>("intro")
  const [direction, setDirection] = useState<"forward" | "back">("forward")
  const [displayName, setDisplayName] = useState(profile.display_name ?? "")
  const [email, setEmail] = useState(profile.email ?? "")
  const [provider, setProvider] = useState<ProviderId>(
    settings.selected_provider
  )
  const [apiKey, setApiKey] = useState("")
  const [showApiKey, setShowApiKey] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const providerRefs = useRef<Array<HTMLButtonElement | null>>([])

  const selectedProvider = settings.providers.find(
    (item) => item.id === provider
  )

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

  const continueFromProfile = () => {
    const trimmedName = displayName.trim()
    const trimmedEmail = email.trim()
    if (!trimmedName) {
      setError("Enter your name.")
      return
    }
    if (trimmedName.length > 100) {
      setError("Keep your name under 100 characters.")
      return
    }
    if (!isValidEmail(trimmedEmail)) {
      setError("Enter a valid email address.")
      return
    }
    changeStep("model", "forward")
  }

  const selectProvider = (nextProvider: ProviderId) => {
    setProvider(nextProvider)
    setApiKey("")
    setShowApiKey(false)
    setError(null)
  }

  const submit = async () => {
    if (!selectedProvider) {
      setError("Choose a model provider.")
      return
    }
    const trimmedKey = apiKey.trim()
    if (!selectedProvider.configured && !trimmedKey) {
      setError(`Add an API key for ${selectedProvider.name}.`)
      return
    }

    setError(null)
    setSubmitting(true)
    try {
      await onComplete({
        displayName: displayName.trim(),
        email: email.trim(),
        provider,
        apiKey: selectedProvider.configured ? "" : trimmedKey,
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
          <div className="onboarding-step" aria-live="polite">
            {stepLabel}
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
                    aria-label="Get started"
                    tabIndex={-1}
                  >
                    <span>Get started</span>
                  </h1>
                  <p className="onboarding-intro-copy">
                    Set up your profile and connect a model.
                  </p>
                  <button
                    className="onboarding-primary-action"
                    type="button"
                    onClick={() => changeStep("profile", "forward")}
                  >
                    <span>Get started</span>
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
                      continueFromProfile()
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
                        required
                        value={email}
                        autoComplete="email"
                        onChange={(event) => setEmail(event.target.value)}
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
                        onClick={() => changeStep("intro", "back")}
                      >
                        Back
                      </button>
                      <button
                        className="onboarding-primary-action"
                        type="submit"
                      >
                        Continue
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
                      aria-label="Model provider"
                    >
                      {settings.providers.map((item, index) => (
                        <button
                          key={item.id}
                          ref={(element) => {
                            providerRefs.current[index] = element
                          }}
                          className={`onboarding-provider ${item.id === provider ? "is-selected" : ""}`}
                          type="button"
                          role="radio"
                          aria-checked={item.id === provider}
                          tabIndex={item.id === provider ? 0 : -1}
                          onClick={() => selectProvider(item.id)}
                          onKeyDown={(event) => {
                            let nextIndex: number | null = null
                            if (
                              event.key === "ArrowDown" ||
                              event.key === "ArrowRight"
                            ) {
                              nextIndex =
                                (index + 1) % settings.providers.length
                            } else if (
                              event.key === "ArrowUp" ||
                              event.key === "ArrowLeft"
                            ) {
                              nextIndex =
                                (index - 1 + settings.providers.length) %
                                settings.providers.length
                            } else if (event.key === "Home") {
                              nextIndex = 0
                            } else if (event.key === "End") {
                              nextIndex = settings.providers.length - 1
                            }

                            if (nextIndex === null) return
                            event.preventDefault()
                            const nextProvider = settings.providers[nextIndex]
                            if (!nextProvider) return
                            selectProvider(nextProvider.id)
                            providerRefs.current[nextIndex]?.focus()
                          }}
                        >
                          <span>{item.name}</span>
                          <span>{item.model}</span>
                          <span>
                            {item.configured
                              ? "Configured"
                              : "API key required"}
                          </span>
                        </button>
                      ))}
                    </div>

                    {selectedProvider && !selectedProvider.configured ? (
                      <label className="onboarding-field">
                        <span>{selectedProvider.name} API key</span>
                        <span className="onboarding-key-input">
                          <KeyRound size={15} aria-hidden="true" />
                          <input
                            aria-label={`${selectedProvider.name} API key`}
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
