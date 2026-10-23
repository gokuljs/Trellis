import type { RunSummary } from "@/lib/app-types"
import type { RuntimeRunEvent } from "@/lib/runtime-client"

const summaryEvents = new Set([
  "run.queued",
  "run.started",
  "run.resumed",
  "run.recovering",
  "run.cancellation_requested",
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.interrupted",
  "model.usage",
  "model.completed",
  "tool.call",
  "tool.result",
  "tool.approval_requested",
  "tool.approval_decided",
])

export function updatesRunSummary(event: RuntimeRunEvent): boolean {
  return (
    summaryEvents.has(event.eventType) ||
    (event.eventType === "run.phase" &&
      typeof event.data.model_call_id === "string")
  )
}

export function activityTotals(
  events: RuntimeRunEvent[],
  summary: RunSummary | null | undefined,
  runId?: string
) {
  const matching =
    summary && (!runId || summary.run_id === runId) ? summary : null
  const updating = Boolean(
    matching &&
    events.some(
      (event) =>
        event.runId === matching.run_id &&
        event.sequence > matching.last_sequence &&
        updatesRunSummary(event)
    )
  )
  return {
    modelCalls: matching?.model_call_count ?? null,
    toolCalls: matching?.tool_call_count ?? null,
    totalTokens: matching?.total_tokens ?? null,
    totalCost: matching?.estimated_cost_usd ?? null,
    usageComplete: Boolean(matching?.usage_complete && !updating),
    costComplete: Boolean(matching?.cost_complete && !updating),
    updating,
  }
}

export function newestRunSummary(
  runId: string | undefined,
  ...summaries: (RunSummary | null | undefined)[]
): RunSummary | undefined {
  return summaries
    .filter((summary): summary is RunSummary =>
      Boolean(summary && summary.run_id === runId)
    )
    .sort((left, right) => right.last_sequence - left.last_sequence)[0]
}

export function coalesceSummaryRefresh(refresh: (sessionId: string) => void) {
  const pending = new Map<string, ReturnType<typeof setTimeout>>()
  return {
    schedule(sessionId: string, event: RuntimeRunEvent) {
      if (!updatesRunSummary(event) || pending.has(sessionId)) return
      pending.set(
        sessionId,
        setTimeout(() => {
          pending.delete(sessionId)
          refresh(sessionId)
        }, 1000)
      )
    },
    dispose() {
      for (const timer of pending.values()) clearTimeout(timer)
      pending.clear()
    },
  }
}
