import type { KeyboardEvent } from "react"
import { Plus, Send } from "lucide-react"

import type { ModelStatus } from "@/lib/app-types"

type BudgetPreset = "conservative" | "longer"

type ComposerProps = {
  value: string
  placeholder: string
  onChange: (value: string) => void
  onSubmit: () => void
  disabled?: boolean
  modelLabel: string
  models: ModelStatus[]
  showModelPicker: boolean
  selectedModelId: string
  onModelChange: (modelId: string) => void
  budgetPreset: BudgetPreset
  onBudgetChange: (preset: BudgetPreset) => void
}

export function Composer({
  value,
  placeholder,
  onChange,
  onSubmit,
  disabled = false,
  modelLabel,
  models,
  showModelPicker,
  selectedModelId,
  onModelChange,
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
          <button
            className="composer-add"
            aria-label="Attach"
            disabled={disabled}
          >
            <Plus size={18} strokeWidth={1.6} aria-hidden="true" />
          </button>
          <div className="composer-tools">
            {showModelPicker ? (
              <select
                className="composer-select"
                aria-label="Run model"
                value={selectedModelId}
                disabled={disabled}
                onChange={(event) => onModelChange(event.target.value)}
              >
                {models.map((model) => (
                  <option
                    key={model.id}
                    value={model.id}
                    disabled={model.requires_api_key && !model.configured}
                  >
                    {model.name}
                    {model.requires_api_key && !model.configured
                      ? " — add API key in Settings"
                      : ""}
                  </option>
                ))}
              </select>
            ) : (
              <span className="model-label">{modelLabel}</span>
            )}
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
