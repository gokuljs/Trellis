import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query"

import { RunActivity } from "@/components/run-activity"
import { eventsFromPages } from "@/lib/activity-history"
import { runEventsQueryOptions } from "@/lib/server-queries"
import type { RuntimeRunInfo } from "@/lib/runtime-client"

export function SavedRunActivity({
  sessionId,
  runId,
  runInfo,
  label = "Saved run activity",
}: {
  sessionId: string
  runId: string
  runInfo: RuntimeRunInfo | null
  label?: string
}) {
  const client = useQueryClient()
  const options = runEventsQueryOptions(sessionId, runId)
  const history = useInfiniteQuery(options)
  const events = eventsFromPages(history.data?.pages ?? [], runId)
  const firstCursor = history.data?.pageParams[0]
  const earlierEvicted = typeof firstCursor === "number" && firstCursor > 0
  return (
    <div>
      {history.isPending ? (
        <p className="run-activity-load-state" role="status">
          Loading activity…
        </p>
      ) : null}
      {events.length ? (
        <RunActivity
          label={label}
          events={events}
          runInfo={runInfo}
          pending={false}
          approvalPendingToolId={null}
          approvalError={null}
          onApprovalDecision={() => undefined}
        />
      ) : !history.isPending && !history.error ? (
        <p className="run-activity-load-state">
          Activity is no longer available.
        </p>
      ) : null}
      {history.error ? (
        <p className="run-activity-load-state" role="alert">
          Could not load activity.{" "}
          <button
            type="button"
            onClick={() => void history.refetch()}
            disabled={history.isFetching}
          >
            Try again
          </button>
        </p>
      ) : null}
      {history.hasNextPage || earlierEvicted ? (
        <nav className="history-pagination" aria-label={`${label} pages`}>
          <div className="history-pagination-actions">
            {history.hasNextPage ? (
              <button
                type="button"
                onClick={() => void history.fetchNextPage()}
                disabled={history.isFetching}
              >
                {history.isFetching ? "Loading…" : "More activity"}
              </button>
            ) : null}
            {earlierEvicted ? (
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
                Jump to start
              </button>
            ) : null}
          </div>
          {earlierEvicted ? <p>Showing a window of up to 500 events.</p> : null}
        </nav>
      ) : null}
    </div>
  )
}
