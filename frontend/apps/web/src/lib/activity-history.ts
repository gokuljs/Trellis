import type { RunSummary, SavedRunEvent } from "@/lib/app-types"
import type {
  RuntimeRunEvent,
  RuntimeRunInfo,
  TurnRunActivity,
  RunAttemptActivity,
} from "@/lib/runtime-client"

export function runInfoFromSummary(summary: RunSummary): RuntimeRunInfo {
  return {
    runId: summary.run_id,
    runMode: summary.run_mode,
    status: summary.status,
    retryAllowed: summary.retry_allowed,
    recoveryReason: summary.recovery_reason,
    uncertainToolCallId: summary.uncertain_tool_call_id,
    modelId: summary.model_id,
    budgetPreset: summary.budget_preset,
    limits: {
      maxModelCalls: summary.max_model_calls,
      maxToolCalls: summary.max_tool_calls,
      maxTotalTokens: summary.max_total_tokens,
      maxCostUsd: summary.max_cost_usd,
      deadlineAt: summary.deadline_at,
    },
  }
}

export function activitiesFromSummaries(
  summaries: RunSummary[]
): Record<string, TurnRunActivity> {
  const byTurn = new Map<string, RunSummary[]>()
  const ordered = [
    ...new Map(summaries.map((summary) => [summary.run_id, summary])).values(),
  ].sort(
    (left, right) =>
      left.created_at.localeCompare(right.created_at) ||
      left.run_id.localeCompare(right.run_id)
  )
  for (const summary of ordered) {
    const attempts = byTurn.get(summary.turn_id) ?? []
    attempts.push(summary)
    byTurn.set(summary.turn_id, attempts)
  }
  return Object.fromEntries(
    [...byTurn].map(([turnId, attempts]) => {
      const latest = attempts.at(-1)!
      return [
        turnId,
        {
          events: [],
          runInfo: runInfoFromSummary(latest),
          summary: latest,
          priorAttempts: attempts
            .slice(0, -1)
            .map((summary): RunAttemptActivity => ({
              runId: summary.run_id,
              summary,
              runInfo: runInfoFromSummary(summary),
              events: null,
            })),
        },
      ]
    })
  )
}

function savedRunEvent(event: SavedRunEvent): RuntimeRunEvent {
  return {
    runId: event.run_id,
    sequence: event.sequence,
    eventType: event.event_type,
    eventVersion: event.event_version,
    data: event.data,
    createdAt: event.created_at,
  }
}

export function eventsFromPages(
  pages: { items: SavedRunEvent[] }[],
  runId: string
): RuntimeRunEvent[] {
  return [
    ...new Map(
      pages
        .flatMap((page) => page.items)
        .filter((event) => event.run_id === runId)
        .map((event) => [event.sequence, savedRunEvent(event)])
    ).values(),
  ].sort((left, right) => left.sequence - right.sequence)
}

export function startNextAttempt(previous?: TurnRunActivity): TurnRunActivity {
  const priorAttempts = [...(previous?.priorAttempts ?? [])].map((attempt) => ({
    ...attempt,
    events: null,
  }))
  const runId = previous?.runInfo?.runId ?? previous?.summary?.run_id
  if (runId && !priorAttempts.some((attempt) => attempt.runId === runId)) {
    priorAttempts.push({
      runId,
      summary: previous?.summary ?? null,
      runInfo: previous?.runInfo ?? null,
      events: null,
    })
  }
  return { events: [], runInfo: null, priorAttempts: priorAttempts.slice(-500) }
}

export function retainWindowActivities(
  activities: Record<string, TurnRunActivity>,
  visibleTurns: ReadonlySet<string>,
  activeRunId?: string | null,
  keepTurnId?: string
): Record<string, TurnRunActivity> {
  return Object.fromEntries(
    Object.entries(activities)
      .filter(
        ([turnId, activity]) =>
          visibleTurns.has(turnId) ||
          turnId === keepTurnId ||
          Boolean(activeRunId && activity.runInfo?.runId === activeRunId)
      )
      .map(([turnId, activity]) => {
        if (
          turnId === keepTurnId ||
          (activeRunId && activity.runInfo?.runId === activeRunId)
        )
          return [turnId, activity]
        const first = activity.events.find(
          (event) =>
            event.eventType === "run.queued" ||
            event.eventType === "run.started"
        )
        const latest = activity.latestEvent ?? activity.events.at(-1)
        return [
          turnId,
          {
            ...activity,
            events: first
              ? [
                  first,
                  ...(latest && latest.sequence !== first.sequence
                    ? [latest]
                    : []),
                ]
              : latest
                ? [latest]
                : [],
            latestEvent: latest,
            priorAttempts: activity.priorAttempts?.map((attempt) => ({
              ...attempt,
              events: null,
            })),
          },
        ]
      })
  )
}
