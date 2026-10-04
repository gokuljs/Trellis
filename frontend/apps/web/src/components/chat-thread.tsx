import { RotateCcw, Square } from "lucide-react"
import Markdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"

import { TrellisMark } from "@/components/trellis-mark"
import type { Message, Session } from "@/lib/app-types"

type ChatThreadProps = {
  session: Session
  messages: Message[]
  pending: boolean
  streamingText: string | null
  error: string | null
  canRetry: boolean
  canCancel: boolean
  cancellationPending: boolean
  onRetry: () => void
  onCancel: () => void
}

function safeImageSource(source: string | undefined) {
  if (!source) return undefined

  try {
    const imageUrl = new URL(source)
    if (imageUrl.protocol === "http:" || imageUrl.protocol === "https:") {
      return imageUrl.href
    }
  } catch {
    return undefined
  }

  return undefined
}

const markdownComponents: Components = {
  img({ src, alt }) {
    const safeSource = safeImageSource(src)
    if (!safeSource) return null

    return (
      <img src={safeSource} alt={alt ?? ""} loading="lazy" decoding="async" />
    )
  },
  table({ children }) {
    return (
      <div className="thread-markdown-table-scroll">
        <table>{children}</table>
      </div>
    )
  },
}

function MessageMarkdown({ content }: { content: string }) {
  return (
    <div className="thread-markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={markdownComponents}
      >
        {content}
      </Markdown>
    </div>
  )
}

export function ChatThread({
  session,
  messages,
  pending,
  streamingText,
  error,
  canRetry,
  canCancel,
  cancellationPending,
  onRetry,
  onCancel,
}: ChatThreadProps) {
  return (
    <div className="chat-thread" aria-live="polite">
      <header className="sr-only">
        <div className="utility-kicker">LOCAL SESSION</div>
        <h1>{session.title}</h1>
        <span>{session.message_count} saved messages</span>
      </header>

      <div className="thread-messages">
        {messages.map((message) => (
          <article
            className={`thread-message ${message.role}`}
            key={message.id}
          >
            <div className="thread-message-copy">
              <div className="thread-message-meta">
                {message.role === "assistant" ? (
                  <span className="thread-agent-mark" aria-hidden="true">
                    <TrellisMark size={17} />
                  </span>
                ) : null}
                <span>{message.role === "assistant" ? "Trellis" : "You"}</span>
                {message.model ? <span>{message.model}</span> : null}
              </div>
              <MessageMarkdown content={message.content} />
            </div>
          </article>
        ))}

        {pending && streamingText !== null ? (
          <article
            className="thread-message assistant pending"
            aria-label="Assistant response streaming"
          >
            <div className="thread-message-copy">
              <div className="thread-message-meta">
                <span className="thread-agent-mark" aria-hidden="true">
                  <TrellisMark size={17} />
                </span>
                <span>Trellis</span>
              </div>
              <MessageMarkdown content={streamingText} />
            </div>
          </article>
        ) : pending ? (
          <article
            className="thread-message assistant pending"
            aria-label="Assistant response pending"
          >
            <div className="thread-message-copy">
              <div className="thread-message-meta">
                <span className="thread-agent-mark" aria-hidden="true">
                  <TrellisMark size={17} />
                </span>
                <span>Trellis</span>
              </div>
              <div className="thinking-pulse">
                <span />
                <span />
                <span />
              </div>
            </div>
          </article>
        ) : null}
      </div>

      {error ? (
        <div className="chat-error" role="alert">
          <span>{error}</span>
          {canRetry ? (
            <button type="button" onClick={onRetry}>
              <RotateCcw size={13} aria-hidden="true" /> Retry
            </button>
          ) : null}
        </div>
      ) : null}
      {canCancel ? (
        <button
          type="button"
          onClick={onCancel}
          className="cancel-run-button"
          disabled={cancellationPending}
        >
          <Square size={13} aria-hidden="true" />
          {cancellationPending ? "Stopping…" : "Stop generating"}
        </button>
      ) : null}
    </div>
  )
}
