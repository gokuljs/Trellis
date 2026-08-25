import type { KeyboardEvent } from "react"
import {
  ChevronDown,
  Plus,
  Send,
} from "lucide-react"

type ComposerProps = {
  value: string
  placeholder: string
  onChange: (value: string) => void
  onSubmit: () => void
}

export function Composer({ value, placeholder, onChange, onSubmit }: ComposerProps) {
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      onSubmit()
    }
  }

  return (
    <div className="composer-wrap">
      <div className="composer-box">
        <button className="composer-add" aria-label="Attach"><Plus size={16} strokeWidth={1.6} aria-hidden="true" /></button>
        <textarea
          aria-label="Message"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          rows={1}
        />
        <div className="composer-tools">
          <span className="model-label">GPT-5.5 · Med <ChevronDown size={11} aria-hidden="true" /></span>
          <button className={`send-button ${value.trim() ? "ready" : ""}`} aria-label="Send" onClick={onSubmit}>
            {value.trim() ? <Send size={14} aria-hidden="true" /> : <span className="voice-orb" aria-hidden="true">◔</span>}
          </button>
        </div>
      </div>
      <div className="composer-footnote">Trellis can make mistakes. Check important info.</div>
    </div>
  )
}
