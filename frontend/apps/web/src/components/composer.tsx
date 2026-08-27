import type { KeyboardEvent } from "react"
import { ChevronDown, Plus, Send } from "lucide-react"

type ComposerProps = {
  value: string
  placeholder: string
  onChange: (value: string) => void
  onSubmit: () => void
  disabled?: boolean
  modelLabel: string
}

export function Composer({
  value,
  placeholder,
  onChange,
  onSubmit,
  disabled = false,
  modelLabel,
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
        <button
          className="composer-add"
          aria-label="Attach"
          disabled={disabled}
        >
          <Plus size={16} strokeWidth={1.6} aria-hidden="true" />
        </button>
        <textarea
          aria-label="Message"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          rows={1}
          disabled={disabled}
        />
        <div className="composer-tools">
          <span className="model-label">
            {modelLabel} <ChevronDown size={11} aria-hidden="true" />
          </span>
          <button
            className={`send-button ${value.trim() && !disabled ? "ready" : ""}`}
            aria-label="Send"
            disabled={disabled || !value.trim()}
            onClick={onSubmit}
          >
            {value.trim() ? (
              <Send size={14} aria-hidden="true" />
            ) : (
              <span className="voice-orb" aria-hidden="true">
                ◔
              </span>
            )}
          </button>
        </div>
      </div>
      <div className="composer-footnote">
        Trellis can make mistakes. Check important info.
      </div>
    </div>
  )
}
