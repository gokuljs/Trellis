import type { KeyboardEvent } from "react"
import {
  ChevronDown,
  Mic,
  Plus,
  Send,
  VolumeX,
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
        <button className="composer-add" aria-label="Attach"><Plus size={16} strokeWidth={1.6} /></button>
        <textarea
          aria-label="Message"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          rows={1}
        />
        <div className="composer-tools">
          <span className="model-label">GPT-5.5 · Med <ChevronDown size={11} /></span>
          <button className="composer-tool" aria-label="Voice input"><Mic size={14} /></button>
          <button className="composer-tool" aria-label="Mute"><VolumeX size={14} /></button>
          <button className={`send-button ${value.trim() ? "ready" : ""}`} aria-label="Send" onClick={onSubmit}>
            {value.trim() ? <Send size={14} /> : <span className="voice-orb">◔</span>}
          </button>
        </div>
      </div>
      <div className="composer-footnote">Name can make mistakes. Check important info.</div>
    </div>
  )
}
