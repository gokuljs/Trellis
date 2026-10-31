import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react"
import { ArrowUp } from "lucide-react"
import { BEND, MetalFx, useMetalBend } from "metal-fx"

import { useTheme } from "@/components/theme-provider"
import { ModelSelect } from "@/components/model-select"
import type { ModelStatus, RunMode } from "@/lib/app-types"
import { isModelSelectable } from "@/lib/model-choice"

type BudgetPreset = "conservative" | "longer"

type ComposerProps = {
  value: string
  placeholder: string
  onChange: (value: string) => void
  onSubmit: () => void
  disabled?: boolean
  budgetPreset: BudgetPreset
  onBudgetChange: (preset: BudgetPreset) => void
  models: ModelStatus[]
  selectedModelId: string
  modelSaving: boolean
  onModelChange: (modelId: string) => void
  contextGauge?: ReactNode
  clarification?: ReactNode
  runStatus?: string
  runMode?: RunMode
  onRunModeChange?: (mode: RunMode) => void
  researchAvailable?: boolean
  researchUnavailableReason?: string | null
  answeringClarification?: boolean
  controlsDisabled?: boolean
}

function usePrefersReducedMotion() {
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
  )

  useEffect(() => {
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)")
    const updatePreference = () => setPrefersReducedMotion(mediaQuery.matches)

    updatePreference()
    mediaQuery.addEventListener("change", updatePreference)
    return () => mediaQuery.removeEventListener("change", updatePreference)
  }, [])

  return prefersReducedMotion
}

export function Composer({
  value,
  placeholder,
  onChange,
  onSubmit,
  disabled = false,
  budgetPreset,
  onBudgetChange,
  models,
  selectedModelId,
  modelSaving,
  onModelChange,
  contextGauge,
  clarification,
  runStatus,
  runMode = "chat",
  onRunModeChange,
  researchAvailable = false,
  researchUnavailableReason,
  answeringClarification = false,
  controlsDisabled = disabled,
}: ComposerProps) {
  const { resolvedTheme } = useTheme()
  const prefersReducedMotion = usePrefersReducedMotion()
  const isSendable =
    Boolean(value.trim()) &&
    !disabled &&
    (runMode === "chat" || researchAvailable || answeringClarification) &&
    isModelSelectable(models.find((model) => model.id === selectedModelId))
  const sendRef = useRef<HTMLDivElement>(null)
  const getBendConfig = useCallback(
    () => ({
      ...BEND,
      enabled: isSendable && !prefersReducedMotion,
    }),
    [isSendable, prefersReducedMotion]
  )

  useMetalBend(sendRef, getBendConfig)

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      if (isSendable) onSubmit()
    }
  }

  return (
    <div className="composer-wrap">
      {clarification}
      <div className="composer-box">
        <textarea
          aria-label="Message"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          rows={1}
          disabled={disabled}
        />
        <div className="composer-toolbar">
          <div className="composer-tools">
            <ModelSelect
              models={models}
              selectedModelId={selectedModelId}
              disabled={controlsDisabled}
              saving={modelSaving}
              onChange={onModelChange}
            />
            <select
              className="composer-select"
              aria-label="Run mode"
              value={runMode}
              disabled={controlsDisabled}
              title={
                !researchAvailable
                  ? (researchUnavailableReason ?? "Research is unavailable")
                  : undefined
              }
              onChange={(event) =>
                onRunModeChange?.(event.target.value as RunMode)
              }
            >
              <option value="chat">Chat</option>
              <option value="research" disabled={!researchAvailable}>
                Research
              </option>
            </select>
            <select
              className="composer-select"
              aria-label="Run budget"
              value={budgetPreset}
              disabled={controlsDisabled}
              onChange={(event) =>
                onBudgetChange(event.target.value as BudgetPreset)
              }
            >
              <option value="conservative">Standard</option>
              <option value="longer">Extended</option>
            </select>
            {contextGauge}
            <MetalFx
              ref={sendRef}
              className="send-button-metal"
              variant="circle"
              preset="chromatic"
              strength={1}
              innerShadow
              theme={resolvedTheme}
              paused={prefersReducedMotion || !isSendable}
              disableGlow={prefersReducedMotion || !isSendable}
            >
              <button
                className="send-button"
                aria-label={
                  answeringClarification ? "Send clarification" : "Send"
                }
                disabled={!isSendable}
                onClick={onSubmit}
              >
                <ArrowUp size={14} aria-hidden="true" />
              </button>
            </MetalFx>
          </div>
        </div>
      </div>
      {!answeringClarification ? (
        <div
          className="composer-footnote"
          role={runStatus ? "status" : undefined}
        >
          {runStatus ??
            (runMode === "research"
              ? budgetPreset === "longer"
                ? "Research limits: 20 minutes, 600,000 tokens, $15 estimated model cost, 16 searches, 40 pages."
                : "Research limits: 10 minutes, 300,000 tokens, $6 estimated model cost, 8 searches, 20 pages."
              : "Trellis can make mistakes. Check important info.")}
        </div>
      ) : null}
    </div>
  )
}
