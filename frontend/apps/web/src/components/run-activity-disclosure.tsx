import { ChevronDown } from "lucide-react"
import { useEffect, useId, useRef, useState } from "react"
import { ThinkingOrb } from "thinking-orbs"

import { RunActivity } from "@/components/run-activity"
import { describeActivity } from "@/lib/activity-status"
import type {
  ToolApprovalDecision,
  TurnRunActivity,
} from "@/lib/runtime-client"

type RunActivityDisclosureProps = {
  activity: TurnRunActivity
  pending: boolean
  expanded: boolean
  onToggle: () => void
  onClose: () => void
  onRequestEvents?: () => void
  approvalPendingToolId: string | null
  approvalError: string | null
  onApprovalDecision: (
    toolCallId: string,
    decision: ToolApprovalDecision
  ) => void
}

export function RunActivityDisclosure({
  activity,
  pending,
  expanded,
  onToggle,
  onClose,
  onRequestEvents = () => undefined,
  approvalPendingToolId,
  approvalError,
  onApprovalDecision,
}: RunActivityDisclosureProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const detailsId = useId()
  const [now, setNow] = useState(() => Date.now())
  const status = describeActivity(
    activity.events,
    pending,
    now,
    activity.latestEvent,
    activity.summary
  )
  const available = Boolean(
    activity.events.length ||
    activity.summary ||
    activity.priorAttempts?.length ||
    pending
  )

  useEffect(() => {
    if (status.terminal || (!activity.events.length && !activity.summary))
      return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [status.terminal, activity.events.length, activity.summary])

  useEffect(() => {
    if (!expanded) return
    const handlePointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) onClose()
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose()
    }
    document.addEventListener("pointerdown", handlePointerDown)
    document.addEventListener("keydown", handleKeyDown)
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown)
      document.removeEventListener("keydown", handleKeyDown)
    }
  }, [expanded, onClose])

  return (
    <div className="run-activity-disclosure" ref={rootRef}>
      {available ? (
        <button
          className="run-activity-trigger"
          type="button"
          aria-expanded={expanded}
          aria-controls={expanded ? detailsId : undefined}
          onClick={onToggle}
        >
          <span className="run-activity-trigger-main">
            <ThinkingOrb
              state={status.orb}
              size={20}
              theme="auto"
              paused={status.terminal}
              aria-hidden="true"
            />
            <span>{status.label}</span>
          </span>
          <span className="run-activity-trigger-end">
            {status.duration ? <time>{status.duration}</time> : null}
            <ChevronDown size={14} aria-hidden="true" />
          </span>
        </button>
      ) : (
        <div className="run-activity-trigger unavailable">
          <span className="run-activity-trigger-main">
            <ThinkingOrb
              state="breathing"
              size={20}
              theme="auto"
              paused
              aria-hidden="true"
            />
            <span>Activity unavailable</span>
          </span>
        </div>
      )}
      {expanded && available ? (
        <div id={detailsId} className="run-activity-expanded">
          {activity.priorAttempts?.map((attempt, index) => (
            <div className="run-activity-attempt" key={attempt.runId}>
              <div className="run-activity-attempt-label">
                Attempt {index + 1}
              </div>
              {attempt.events?.length ? (
                <RunActivity
                  label={`Attempt ${index + 1} activity`}
                  events={attempt.events}
                  runInfo={attempt.runInfo}
                  pending={false}
                  approvalPendingToolId={null}
                  approvalError={null}
                  onApprovalDecision={onApprovalDecision}
                />
              ) : (
                <p className="run-activity-load-state">
                  {attempt.error ??
                    (attempt.loading
                      ? "Loading activity…"
                      : "Activity unavailable")}
                </p>
              )}
            </div>
          ))}
          {activity.priorAttempts?.length ? (
            <div className="run-activity-attempt-label">Latest attempt</div>
          ) : null}
          {activity.events.length ? (
            <RunActivity
              events={activity.events}
              runInfo={activity.runInfo}
              pending={pending}
              approvalPendingToolId={approvalPendingToolId}
              approvalError={approvalError}
              onApprovalDecision={onApprovalDecision}
            />
          ) : (
            <p className="run-activity-load-state">
              {activity.eventsError ??
                (activity.eventsLoading
                  ? "Loading activity…"
                  : "Waiting for activity…")}
            </p>
          )}
          {activity.eventsError ||
          activity.priorAttempts?.some((attempt) => attempt.error) ? (
            <button
              type="button"
              className="run-activity-retry"
              onClick={onRequestEvents}
            >
              Retry loading
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
