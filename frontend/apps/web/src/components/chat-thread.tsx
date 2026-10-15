import { RotateCcw, Square } from "lucide-react"
import { useState } from "react"
import Markdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"
import { ThinkingOrb } from "thinking-orbs"

import { TrellisMark } from "@trellis/ui/icons/trellis-mark"
import { HistoryPagination } from "@/components/history-pagination"
import { RunActivityDisclosure } from "@/components/run-activity-disclosure"
import type {
  AssistantOutput,
  Message,
  Session,
  TimelineRow,
} from "@/lib/app-types"
import { outputStatusLabel, threadMessages } from "@/lib/assistant-outputs"
import type {
  ToolApprovalDecision,
  TurnRunActivity,
} from "@/lib/runtime-client"

type ChatThreadProps = {
  session: Session
  messages: Message[]
  assistantOutputs?: AssistantOutput[]
  timelineOrder?: TimelineRow[]
  hasOlderHistory?: boolean
  browsingHistory?: boolean
  historyLoading?: boolean
  historyError?: string | null
  onLoadOlderHistory?: () => void
  onJumpToLatest?: () => void
  pending: boolean
  streamingText: string | null
  error: string | null
  activityHistoryError?: string | null
  onRetryActivityHistory?: () => void
  canRetry: boolean
  canReconnect?: boolean
  canCancel: boolean
  cancellationPending: boolean
  runActivities?: Record<string, TurnRunActivity>
  onRequestActivityEvents?: (turnId: string) => void
  activeRunTurnId?: string | null
  approvalPendingToolId?: string | null
  approvalError?: string | null
  onApprovalDecision?: (
    toolCallId: string,
    decision: ToolApprovalDecision
  ) => void
  onRetry: () => void
  onReconnect?: () => void
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
  assistantOutputs = [],
  timelineOrder,
  hasOlderHistory = false,
  browsingHistory = false,
  historyLoading = false,
  historyError = null,
  onLoadOlderHistory = () => undefined,
  onJumpToLatest = () => undefined,
  pending,
  streamingText,
  error,
  activityHistoryError = null,
  onRetryActivityHistory = () => undefined,
  canRetry,
  canReconnect = false,
  canCancel,
  cancellationPending,
  runActivities = {},
  onRequestActivityEvents = () => undefined,
  activeRunTurnId = null,
  approvalPendingToolId = null,
  approvalError = null,
  onApprovalDecision = () => undefined,
  onRetry,
  onReconnect = () => undefined,
  onCancel,
}: ChatThreadProps) {
  const [openActivityTurnId, setOpenActivityTurnId] = useState<string | null>(
    null
  )
  const runOrderByTurn = Object.fromEntries(
    Object.entries(runActivities).map(([turnId, activity]) => [
      turnId,
      [
        ...(activity.priorAttempts ?? []).map((attempt) => attempt.runId),
        activity.runInfo?.runId ?? activity.summary?.run_id,
      ].filter((runId): runId is string => typeof runId === "string"),
    ])
  )
  const displayMessages = threadMessages(
    messages,
    assistantOutputs,
    runOrderByTurn,
    timelineOrder
  )
  const firstAssistantByTurn = new Map<string, string>()
  for (const message of displayMessages) {
    if (
      message.role === "assistant" &&
      !firstAssistantByTurn.has(message.turn_id)
    )
      firstAssistantByTurn.set(message.turn_id, message.id)
  }
  const activeActivity = activeRunTurnId ? runActivities[activeRunTurnId] : null
  const latestApprovalState = activeActivity?.events.findLast((event) =>
    [
      "tool.approval_requested",
      "tool.approval_decided",
      "run.cancellation_requested",
      "run.completed",
      "run.failed",
      "run.cancelled",
      "run.interrupted",
    ].includes(event.eventType)
  )
  const waitingForApproval =
    latestApprovalState?.eventType === "tool.approval_requested"
  const activityForTurn = (turnId: string) => {
    const activity = runActivities[turnId] ?? { events: [], runInfo: null }
    const active = activeRunTurnId === turnId
    return (
      <RunActivityDisclosure
        key={`activity-${turnId}`}
        sessionId={session.id}
        turnId={turnId}
        activity={activity}
        pending={pending && active}
        expanded={openActivityTurnId === turnId}
        onToggle={() => {
          if (openActivityTurnId !== turnId) onRequestActivityEvents(turnId)
          setOpenActivityTurnId(openActivityTurnId === turnId ? null : turnId)
        }}
        onClose={() => setOpenActivityTurnId(null)}
        onRequestEvents={() => onRequestActivityEvents(turnId)}
        approvalPendingToolId={active ? approvalPendingToolId : null}
        approvalError={active ? approvalError : null}
        onApprovalDecision={onApprovalDecision}
      />
    )
  }

  return (
    <div className="chat-thread" aria-live="polite">
      <header className="sr-only">
        <div className="utility-kicker">Saved chat</div>
        <h1>{session.title}</h1>
        <span>{session.message_count} saved messages</span>
      </header>

      <HistoryPagination
        label="Chat history"
        olderLabel="Load older messages"
        latestLabel="Jump to latest"
        hasOlder={hasOlderHistory}
        browsing={browsingHistory}
        loading={historyLoading}
        disabled={pending}
        error={historyError}
        onOlder={onLoadOlderHistory}
        onLatest={onJumpToLatest}
      />
      <div className="thread-messages">
        {displayMessages.flatMap((message) => [
          message.role === "assistant" &&
          firstAssistantByTurn.get(message.turn_id) === message.id
            ? activityForTurn(message.turn_id)
            : null,
          <article
            key={message.id}
            className={[
              "thread-message",
              message.role,
              ...(message.output?.status === "streaming" ? ["pending"] : []),
            ].join(" ")}
            aria-label={
              message.role === "user"
                ? "Your message"
                : message.output
                  ? `Assistant response${message.attemptNumber ? `, attempt ${message.attemptNumber}` : ""}, ${outputStatusLabel(message.output) || "completed"}`
                  : undefined
            }
          >
            <div className="thread-message-copy">
              <div className="thread-message-meta">
                {message.role === "assistant" ? (
                  <span className="thread-agent-mark" aria-hidden="true">
                    <TrellisMark size={17} />
                  </span>
                ) : null}
                <span>
                  {message.role === "assistant" ? "Trellis" : "You"}
                  {message.attemptNumber
                    ? ` · Attempt ${message.attemptNumber}`
                    : ""}
                  {message.output && outputStatusLabel(message.output)
                    ? ` · ${outputStatusLabel(message.output)}`
                    : ""}
                </span>
              </div>
              <MessageMarkdown content={message.content} />
            </div>
          </article>,
          message.role === "user" && !firstAssistantByTurn.has(message.turn_id)
            ? activityForTurn(message.turn_id)
            : null,
        ])}

        {pending &&
        activeRunTurnId &&
        !displayMessages.some((message) => message.turn_id === activeRunTurnId)
          ? activityForTurn(activeRunTurnId)
          : null}

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
        ) : pending &&
          !waitingForApproval &&
          !activeActivity &&
          !displayMessages.some(
            (message) => message.output?.status === "streaming"
          ) ? (
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
              <ThinkingOrb
                state="working"
                size={20}
                theme="auto"
                aria-hidden="true"
              />
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
          {canReconnect ? (
            <button type="button" onClick={onReconnect}>
              <RotateCcw size={13} aria-hidden="true" /> Reconnect
            </button>
          ) : null}
        </div>
      ) : null}
      {activityHistoryError ? (
        <div className="chat-error" role="alert">
          <span>{activityHistoryError}</span>
          <button
            type="button"
            onClick={onRetryActivityHistory}
            disabled={pending}
          >
            <RotateCcw size={13} aria-hidden="true" /> Retry activity
          </button>
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
