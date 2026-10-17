import { useState } from "react"
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query"

import { SavedRunActivity } from "@/components/saved-run-activity"
import { runInfoFromSummary } from "@/lib/activity-history"
import { runSummariesQueryOptions } from "@/lib/server-queries"
import type { RunSummary } from "@/lib/app-types"
import { newestRunSummary } from "@/lib/activity-summary"

export function SavedTurnActivity({
  sessionId,
  turnId,
  fallback,
}: {
  sessionId: string
  turnId: string
  fallback?: RunSummary | null
}) {
  const client = useQueryClient()
  const options = runSummariesQueryOptions(sessionId, undefined, turnId)
  const history = useInfiniteQuery(options)
  const [chosenRunId, setChosenRunId] = useState<string | null>(null)
  const byRun = new Map<string, RunSummary>()
  for (const summary of [
    ...(fallback?.turn_id === turnId ? [fallback] : []),
    ...(history.data?.pages.flatMap((page) => page.items) ?? []),
  ]) {
    byRun.set(
      summary.run_id,
      newestRunSummary(summary.run_id, byRun.get(summary.run_id), summary)!
    )
  }
  const summaries = [...byRun.values()].sort(
    (left, right) =>
      right.created_at.localeCompare(left.created_at) ||
      right.run_id.localeCompare(left.run_id)
  )
  const selected =
    summaries.find((summary) => summary.run_id === chosenRunId) ?? summaries[0]
  const firstCursor = history.data?.pageParams[0]
  const latestEvicted = typeof firstCursor === "number" && firstCursor > 0
  return (
    <div>
      {history.isPending ? (
        <p className="run-activity-load-state" role="status">
          Loading attempts…
        </p>
      ) : null}
      {history.error ? (
        <p className="run-activity-load-state" role="alert">
          Could not load attempts.{" "}
          <button
            type="button"
            onClick={() => void history.refetch()}
            disabled={history.isFetching}
          >
            Try again
          </button>
        </p>
      ) : null}
      {summaries.length > 1 ? (
        <label className="run-activity-attempt-picker">
          Run attempt{" "}
          <select
            value={selected.run_id}
            onChange={(event) => setChosenRunId(event.target.value)}
          >
            {summaries.map((summary) => (
              <option key={summary.run_id} value={summary.run_id}>
                {new Date(summary.created_at).toLocaleString()} ·{" "}
                {summary.status} · {summary.run_id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {selected ? (
        <SavedRunActivity
          key={selected.run_id}
          sessionId={sessionId}
          runId={selected.run_id}
          runInfo={runInfoFromSummary(selected)}
          summary={selected}
        />
      ) : !history.isPending && !history.error ? (
        <p className="run-activity-load-state">
          Activity is no longer available.
        </p>
      ) : null}
      {history.hasNextPage || latestEvicted ? (
        <nav className="history-pagination" aria-label="Run attempts history">
          <div className="history-pagination-actions">
            {history.hasNextPage ? (
              <button
                type="button"
                onClick={() => void history.fetchNextPage()}
                disabled={history.isFetching}
              >
                {history.isFetching ? "Loading…" : "Older attempts"}
              </button>
            ) : null}
            {latestEvicted ? (
              <button
                type="button"
                onClick={() =>
                  void client.resetQueries({
                    queryKey: options.queryKey,
                    exact: true,
                  })
                }
                disabled={history.isFetching}
              >
                Latest attempts
              </button>
            ) : null}
          </div>
          {latestEvicted ? (
            <p>Showing a window of up to 500 attempts.</p>
          ) : null}
        </nav>
      ) : null}
    </div>
  )
}
