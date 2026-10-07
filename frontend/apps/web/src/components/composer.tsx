import type { KeyboardEvent } from "react"
import { Send } from "lucide-react"

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
            <button
              className={`send-button ${value.trim() && !disabled ? "ready" : ""}`}
              aria-label="Send"
              disabled={disabled || !value.trim()}
              onClick={onSubmit}
            >
              {value.trim() ? (
                <Send size={15} aria-hidden="true" />
              ) : (
                <span className="voice-orb" aria-hidden="true">
                  ◔
                </span>
              )}
            </button>
          </div>
        </div>
      </div>
      <div className="composer-footnote">
        Trellis can make mistakes. Check important info.
      </div>
    </div>
  )
}
