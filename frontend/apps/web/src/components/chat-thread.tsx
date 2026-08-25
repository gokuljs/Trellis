import { Bot, RotateCcw, UserRound } from "lucide-react"

import type { Message, Session } from "@/lib/app-types"

type ChatThreadProps = {
  session: Session
  messages: Message[]
  pending: boolean
  error: string | null
  canRetry: boolean
  onRetry: () => void
}

export function ChatThread({
  session,
  messages,
  pending,
  error,
  canRetry,
  onRetry,
}: ChatThreadProps) {
  return (
    <div className="chat-thread" aria-live="polite">
      <header className="thread-header">
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
            <span className="thread-node" aria-hidden="true">
              {message.role === "assistant" ? (
                <Bot size={14} />
              ) : (
                <UserRound size={14} />
              )}
            </span>
            <div className="thread-message-copy">
              <div className="thread-message-meta">
                <span>{message.role === "assistant" ? "Trellis" : "You"}</span>
                {message.model ? <span>{message.model}</span> : null}
              </div>
              <p>{message.content}</p>
            </div>
          </article>
        ))}

        {pending ? (
          <article
            className="thread-message assistant pending"
            aria-label="Assistant response pending"
          >
            <span className="thread-node" aria-hidden="true">
              <Bot size={14} />
            </span>
            <div className="thread-message-copy">
              <div className="thread-message-meta">
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
    </div>
  )
}
