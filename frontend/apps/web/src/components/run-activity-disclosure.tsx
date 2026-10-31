import { ChevronDown } from "lucide-react"
import { useEffect, useId, useRef, useState } from "react"
import { ThinkingOrb } from "thinking-orbs"
import { useInfiniteQuery, useQuery } from "@tanstack/react-query"

import { SavedTurnActivity } from "@/components/saved-turn-activity"
import { SavedRunActivity } from "@/components/saved-run-activity"
import { RunActivity } from "@/components/run-activity"
import { ResearchProgress } from "@/components/research-progress"
import { researchQueryOptions } from "@/lib/research"
import { describeActivity, researchOutcomeStatus } from "@/lib/activity-status"
import { runSummariesQueryOptions } from "@/lib/server-queries"
import { newestRunSummary } from "@/lib/activity-summary"
import type {
  ToolApprovalDecision,
  TurnRunActivity,
} from "@/lib/runtime-client"

type RunActivityDisclosureProps = {
  sessionId?: string
  turnId?: string
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
  sessionId,
  turnId,
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
  const [openAttemptId, setOpenAttemptId] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const summaries = useInfiniteQuery({
    ...runSummariesQueryOptions(sessionId ?? "", undefined, turnId),
    enabled: expanded && pending && Boolean(sessionId && turnId),
  })
  const runId =
    activity.runInfo?.runId ??
    activity.latestEvent?.runId ??
    activity.summary?.run_id
  const summary = newestRunSummary(
    runId,
    summaries.data?.pages
      .flatMap((page) => page.items)
      .find((item) => item.run_id === runId),
    activity.summary
  )
  const researchMode =
    activity.runInfo?.runMode === "research" || summary?.run_mode === "research"
  const activityStatus = describeActivity(
    activity.events,
    pending,
    now,
    activity.latestEvent,
    summary,
    activity.runInfo?.status
  )
  const terminalResearch = researchMode && activityStatus.terminal
  const research = useQuery({
    ...researchQueryOptions(sessionId ?? "", runId ?? ""),
    enabled: Boolean(
      (expanded || terminalResearch) && researchMode && sessionId && runId
    ),
    refetchInterval: expanded && pending && researchMode ? 2500 : false,
  })
  const refetchResearch = research.refetch
  useEffect(() => {
    if (terminalResearch && sessionId && runId)
      void refetchResearch({ cancelRefetch: false })
  }, [terminalResearch, sessionId, runId, refetchResearch])
  const status = researchMode
    ? researchOutcomeStatus(activityStatus, runId, research.data)
    : activityStatus
  const available = Boolean(
    (sessionId && turnId) ||
    activity.events.length ||
    activity.summary ||
    activity.priorAttempts?.length ||
    pending
  )

  useEffect(() => {
    if (status.terminal || (!activity.events.length && !summary)) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [status.terminal, activity.events.length, summary])

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
            <span>
              {!pending &&
              !activity.summary &&
              !activity.runInfo?.status &&
              !activity.latestEvent &&
              !activity.events.length
                ? "View activity"
                : status.label}
            </span>
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
        <div
          id={detailsId}
          className="run-activity-expanded"
          role="region"
          aria-label="Activity details"
          tabIndex={0}
        >
          {researchMode ? (
            research.data ? (
              <ResearchProgress research={research.data} />
            ) : (
              <p
                className="run-activity-load-state"
                role={research.error ? "alert" : "status"}
              >
                {research.error
                  ? "Research details could not be loaded."
                  : "Loading research details…"}
                {research.error ? (
                  <button type="button" onClick={() => void research.refetch()}>
                    Retry
                  </button>
                ) : null}
              </p>
            )
          ) : null}
          {sessionId && turnId && !pending ? (
            <SavedTurnActivity
              sessionId={sessionId}
              turnId={turnId}
              fallback={summary}
            />
          ) : (
            <>
              {activity.priorAttempts?.map((attempt, index) => (
                <div className="run-activity-attempt" key={attempt.runId}>
                  <div className="run-activity-attempt-label">
                    Attempt {index + 1}
                  </div>
                  {sessionId ? (
                    <>
                      <button
                        type="button"
                        className="run-activity-retry"
                        aria-expanded={openAttemptId === attempt.runId}
                        onClick={() =>
                          setOpenAttemptId(
                            openAttemptId === attempt.runId
                              ? null
                              : attempt.runId
                          )
                        }
                      >
                        {" "}
                        {openAttemptId === attempt.runId
                          ? "Hide attempt activity"
                          : "View attempt activity"}
                      </button>
                      {openAttemptId === attempt.runId ? (
                        <SavedRunActivity
                          sessionId={sessionId}
                          runId={attempt.runId}
                          runInfo={attempt.runInfo}
                          summary={attempt.summary}
                          label={`Attempt ${index + 1} activity`}
                        />
                      ) : null}
                    </>
                  ) : attempt.events?.length ? (
                    <RunActivity
                      label={`Attempt ${index + 1} activity`}
                      events={attempt.events}
                      runInfo={attempt.runInfo}
                      summary={attempt.summary}
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
              {sessionId &&
              !pending &&
              (activity.summary?.run_id || activity.runInfo?.runId) ? (
                <SavedRunActivity
                  sessionId={sessionId}
                  runId={(activity.summary?.run_id ?? activity.runInfo?.runId)!}
                  runInfo={activity.runInfo}
                  summary={summary}
                />
              ) : activity.events.length || summary || activity.runInfo ? (
                <RunActivity
                  events={activity.events}
                  runInfo={activity.runInfo}
                  summary={summary}
                  research={research.data}
                  pending={pending}
                  summaryError={
                    summaries.error ? "Run totals could not be loaded." : null
                  }
                  onRetrySummary={() => void summaries.refetch()}
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
            </>
          )}
        </div>
      ) : null}
    </div>
  )
}
