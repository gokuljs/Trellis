import "./history-pagination.css"

type HistoryPaginationProps = {
  label: string
  olderLabel: string
  latestLabel: string
  hasOlder: boolean
  browsing: boolean
  loading: boolean
  disabled?: boolean
  error?: string | null
  onOlder: () => void
  onLatest: () => void
}

export function HistoryPagination({
  label,
  olderLabel,
  latestLabel,
  hasOlder,
  browsing,
  loading,
  disabled = false,
  error,
  onOlder,
  onLatest,
}: HistoryPaginationProps) {
  if (!hasOlder && !browsing && !error) return null
  return (
    <nav className="history-pagination" aria-label={label}>
      <div className="history-pagination-actions">
        {hasOlder ? (
          <button
            type="button"
            onClick={onOlder}
            disabled={disabled || loading}
          >
            {loading ? "Loading…" : olderLabel}
          </button>
        ) : null}
        {browsing || (error && !hasOlder) ? (
          <button
            type="button"
            onClick={onLatest}
            disabled={disabled || loading}
          >
            {browsing ? latestLabel : "Try again"}
          </button>
        ) : null}
      </div>
      {browsing ? <p>Showing a window of up to 200 rows.</p> : null}
      {error ? <p role="alert">{error}</p> : null}
    </nav>
  )
}
