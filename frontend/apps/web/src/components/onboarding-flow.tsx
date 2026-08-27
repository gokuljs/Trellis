import { Eye, EyeOff, KeyRound } from "lucide-react"
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

function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "Trellis could not finish setup."
}

function isValidEmail(value: string) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
}

function DitherEdge() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    if (!("WebGL2RenderingContext" in window)) {
      canvas.dataset.fallback = "true"
      return
    }

    let gl: WebGL2RenderingContext | null = null
    try {
      gl = canvas.getContext("webgl2", {
        alpha: true,
        antialias: false,
        depth: false,
        premultipliedAlpha: true,
      })
    } catch {
      canvas.dataset.fallback = "true"
    }

    if (!gl) {
      canvas.dataset.fallback = "true"
      return
    }

    const vertexShader = gl.createShader(gl.VERTEX_SHADER)
    const fragmentShader = gl.createShader(gl.FRAGMENT_SHADER)
    if (!vertexShader || !fragmentShader) {
      canvas.dataset.fallback = "true"
      return
    }

    gl.shaderSource(
      vertexShader,
      `#version 300 es
in vec2 a_position;
out vec2 v_uv;

void main() {
  v_uv = a_position * 0.5 + 0.5;
  gl_Position = vec4(a_position, 0.0, 1.0);
}`
    )
    gl.compileShader(vertexShader)

    gl.shaderSource(
      fragmentShader,
      `#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 out_color;

float bayer4(vec2 cell) {
  int x = int(mod(cell.x, 4.0));
  int y = int(mod(cell.y, 4.0));
  int index = x + y * 4;

  if (index == 0) return 0.0 / 16.0;
  if (index == 1) return 8.0 / 16.0;
  if (index == 2) return 2.0 / 16.0;
  if (index == 3) return 10.0 / 16.0;
  if (index == 4) return 12.0 / 16.0;
  if (index == 5) return 4.0 / 16.0;
  if (index == 6) return 14.0 / 16.0;
  if (index == 7) return 6.0 / 16.0;
  if (index == 8) return 3.0 / 16.0;
  if (index == 9) return 11.0 / 16.0;
  if (index == 10) return 1.0 / 16.0;
  if (index == 11) return 9.0 / 16.0;
  if (index == 12) return 15.0 / 16.0;
  if (index == 13) return 7.0 / 16.0;
  if (index == 14) return 13.0 / 16.0;
  return 5.0 / 16.0;
}

void main() {
  vec2 cell = floor(v_uv * vec2(12.0, 64.0));
  float dot = step(bayer4(cell), 0.42);
  out_color = vec4(vec3(0.82), dot * 0.55);
}`
    )
    gl.compileShader(fragmentShader)

    const program = gl.createProgram()
    if (!program) {
      canvas.dataset.fallback = "true"
      return
    }
    gl.attachShader(program, vertexShader)
    gl.attachShader(program, fragmentShader)
    gl.linkProgram(program)

    if (
      !gl.getShaderParameter(vertexShader, gl.COMPILE_STATUS) ||
      !gl.getShaderParameter(fragmentShader, gl.COMPILE_STATUS) ||
      !gl.getProgramParameter(program, gl.LINK_STATUS)
    ) {
      canvas.dataset.fallback = "true"
      gl.deleteProgram(program)
      gl.deleteShader(vertexShader)
      gl.deleteShader(fragmentShader)
      return
    }

    const buffer = gl.createBuffer()
    const position = gl.getAttribLocation(program, "a_position")
    if (!buffer || position < 0) {
      canvas.dataset.fallback = "true"
      gl.deleteProgram(program)
      gl.deleteShader(vertexShader)
      gl.deleteShader(fragmentShader)
      return
    }

    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
      gl.STATIC_DRAW
    )
    gl.useProgram(program)
    gl.enableVertexAttribArray(position)
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0)
    gl.enable(gl.BLEND)
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)

    const draw = () => {
      const rect = canvas.getBoundingClientRect()
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2)
      const width = Math.max(1, Math.round(rect.width * pixelRatio))
      const height = Math.max(1, Math.round(rect.height * pixelRatio))
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width
        canvas.height = height
      }
      gl!.viewport(0, 0, width, height)
      gl!.clearColor(0, 0, 0, 0)
      gl!.clear(gl!.COLOR_BUFFER_BIT)
      gl!.drawArrays(gl!.TRIANGLES, 0, 6)
    }

    draw()
    const resizeObserver =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(draw)
    resizeObserver?.observe(canvas)

    return () => {
      resizeObserver?.disconnect()
      gl?.deleteBuffer(buffer)
      gl?.deleteProgram(program)
      gl?.deleteShader(vertexShader)
      gl?.deleteShader(fragmentShader)
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      className="onboarding-dither-edge"
      aria-hidden="true"
    />
  )
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
                <DitherEdge />
                <TrellisMark size={22} />
                <h1
                  id="onboarding-intro-heading"
                  ref={headingRef}
                  aria-label="Get started"
                  tabIndex={-1}
                >
                  <span>Get</span>
                  <span>started</span>
                </h1>
                <button
                  className="onboarding-primary-action"
                  type="button"
                  onClick={() => changeStep("profile", "forward")}
                >
                  Start onboarding
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
                    <button className="onboarding-primary-action" type="submit">
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
                    {settings.providers.map((item) => (
                      <button
                        key={item.id}
                        className={`onboarding-provider ${item.id === provider ? "is-selected" : ""}`}
                        type="button"
                        role="radio"
                        aria-checked={item.id === provider}
                        onClick={() => {
                          setProvider(item.id)
                          setApiKey("")
                          setShowApiKey(false)
                          setError(null)
                        }}
                      >
                        <span>{item.name}</span>
                        <span>{item.model}</span>
                        <span>
                          {item.configured ? "Configured" : "API key required"}
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
      </div>
    </main>
  )
}
