import { Maximize2, X } from "lucide-react"
import { useId, useMemo, useRef, useState } from "react"

import "./tool-result.css"

type FormattedResult =
  | { kind: "json"; content: string }
  | { kind: "text"; reason: "plain-text" | "format-limit" }

function formatResult(content: string): FormattedResult {
  if (content.length > 131_072) {
    return { kind: "text", reason: "format-limit" }
  }
  try {
    JSON.parse(content)
  } catch {
    return { kind: "text", reason: "plain-text" }
  }

  const tokens =
    content.match(/"(?:\\.|[^"\\])*"|[{}[\],:]|[^\s{}[\],:]+/g) ?? []
  let result = ""
  let depth = 0
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]
    let addition = token
    if (token === "{" || token === "[") {
      const closing = token === "{" ? "}" : "]"
      if (tokens[index + 1] === closing) {
        addition += closing
        index += 1
      } else {
        depth += 1
        if (depth > 80) return { kind: "text", reason: "format-limit" }
        addition += `\n${"  ".repeat(depth)}`
      }
    } else if (token === "}" || token === "]") {
      depth -= 1
      addition = `\n${"  ".repeat(depth)}${token}`
    } else if (token === ",") {
      addition += `\n${"  ".repeat(depth)}`
    } else if (token === ":") {
      addition += " "
    }
    if (result.length + addition.length > 262_144) {
      return { kind: "text", reason: "format-limit" }
    }
    result += addition
  }
  return { kind: "json", content: result }
}

export function ToolResult({
  content,
  toolName,
}: {
  content: string
  toolName: string
}) {
  const titleId = useId()
  const dialogRef = useRef<HTMLDialogElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [mode, setMode] = useState<"json" | "raw">("json")
  const formatted = useMemo(
    () => (expanded ? formatResult(content) : null),
    [content, expanded]
  )
  const showJson = mode === "json" && formatted?.kind === "json"

  return (
    <div className="tool-result">
      <pre
        className="run-tool-result"
        tabIndex={0}
        aria-label={`${toolName} result preview`}
      >
        {content}
      </pre>
      <button
        ref={triggerRef}
        type="button"
        className="tool-result-expand"
        aria-label={`Expand ${toolName} result`}
        aria-haspopup="dialog"
        title="Expand result"
        onClick={() => {
          const dialog = dialogRef.current
          if (!dialog || dialog.open) return
          setMode("json")
          setExpanded(true)
          dialog.showModal()
        }}
      >
        <Maximize2 size={14} aria-hidden="true" />
      </button>
      <dialog
        ref={dialogRef}
        className="tool-result-dialog"
        aria-labelledby={titleId}
        onClose={() => {
          setExpanded(false)
          triggerRef.current?.focus()
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") event.stopPropagation()
        }}
        onClick={(event) => {
          if (event.target !== event.currentTarget) return
          const bounds = event.currentTarget.getBoundingClientRect()
          if (
            event.clientX < bounds.left ||
            event.clientX > bounds.right ||
            event.clientY < bounds.top ||
            event.clientY > bounds.bottom
          ) {
            event.currentTarget.close()
          }
        }}
      >
        <div className="tool-result-dialog-header">
          <h2 id={titleId}>{toolName} result</h2>
          <button
            type="button"
            className="tool-result-close"
            aria-label="Close tool result"
            onClick={() => dialogRef.current?.close()}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>
        <div className="tool-result-toolbar">
          {formatted?.kind === "json" ? (
            <div
              className="tool-result-modes"
              role="group"
              aria-label="Result format"
            >
              <button
                type="button"
                aria-pressed={mode === "json"}
                onClick={() => setMode("json")}
              >
                JSON
              </button>
              <button
                type="button"
                aria-pressed={mode === "raw"}
                onClick={() => setMode("raw")}
              >
                Raw
              </button>
            </div>
          ) : (
            <span className="tool-result-format-label">
              {formatted?.reason === "format-limit"
                ? "Formatting limit reached. Showing original text."
                : "Plain text"}
            </span>
          )}
        </div>
        <pre
          className="tool-result-output"
          tabIndex={0}
          aria-label={showJson ? "Formatted JSON result" : "Raw tool result"}
        >
          {showJson ? formatted.content : content}
        </pre>
      </dialog>
    </div>
  )
}
