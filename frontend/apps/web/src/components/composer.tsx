import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react"
import { ArrowUp } from "lucide-react"
import { BEND, MetalFx, useMetalBend } from "metal-fx"

import { useTheme } from "@/components/theme-provider"
import { WorkspaceAttachment } from "@/components/workspace-attachment"

type BudgetPreset = "conservative" | "longer"

type ComposerProps = {
  value: string
  placeholder: string
  workspacePath: string | null
  workspaceSessionKey: string
  onChange: (value: string) => void
  onSubmit: () => void
  onPickWorkspace: () => Promise<boolean>
  onSaveWorkspace: (path: string) => Promise<void>
  onRemoveWorkspace: () => Promise<void>
  disabled?: boolean
  budgetPreset: BudgetPreset
  onBudgetChange: (preset: BudgetPreset) => void
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
  workspacePath,
  workspaceSessionKey,
  onChange,
  onSubmit,
  onPickWorkspace,
  onSaveWorkspace,
  onRemoveWorkspace,
  disabled = false,
  budgetPreset,
  onBudgetChange,
}: ComposerProps) {
  const { resolvedTheme } = useTheme()
  const prefersReducedMotion = usePrefersReducedMotion()
  const isSendable = Boolean(value.trim()) && !disabled
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
      if (!disabled) onSubmit()
    }
  }

  return (
    <div className="composer-wrap">
      <div className="composer-box">
        <WorkspaceAttachment
          key={workspaceSessionKey}
          workspacePath={workspacePath}
          disabled={disabled}
          onPickWorkspace={onPickWorkspace}
          onSave={onSaveWorkspace}
          onRemove={onRemoveWorkspace}
        />
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
            <select
              className="composer-select"
              aria-label="Run budget"
              value={budgetPreset}
              disabled={disabled}
              onChange={(event) =>
                onBudgetChange(event.target.value as BudgetPreset)
              }
            >
              <option value="conservative">Standard</option>
              <option value="longer">Extended</option>
            </select>
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
                aria-label="Send"
                disabled={!isSendable}
                onClick={onSubmit}
              >
                <ArrowUp size={18} aria-hidden="true" />
              </button>
            </MetalFx>
          </div>
        </div>
      </div>
      <div className="composer-footnote">
        Trellis can make mistakes. Check important info.
      </div>
    </div>
  )
}
