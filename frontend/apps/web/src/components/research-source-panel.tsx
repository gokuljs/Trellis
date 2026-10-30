import { X } from "lucide-react"
import { useEffect, useId, useRef } from "react"
import { useQuery } from "@tanstack/react-query"

import { ApiError, api } from "@/lib/api"
import type { SourceCitation } from "@/lib/app-types"
import "./research.css"

function sourceUrl(value: string | null) {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === "https:" || url.protocol === "http:"
      ? url.href
      : null
  } catch {
    return null
  }
}

export function ResearchSourcePanel({
  sessionId,
  citation,
  onClose,
}: {
  sessionId: string
  citation: SourceCitation
  onClose: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const source = useQuery({
    queryKey: ["source-lines", sessionId, citation],
    queryFn: ({ signal }) =>
      api.readSource(
        sessionId,
        citation.source_id,
        citation.start_line,
        Math.min(200, citation.end_line - citation.start_line + 1),
        { signal }
      ),
    retry: false,
    staleTime: 0,
  })
  const metadata = source.data?.source
  const unavailable =
    metadata && !["success", "partial"].includes(metadata.status)
  const url = sourceUrl(metadata?.final_url ?? metadata?.requested_url ?? null)

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    dialog.addEventListener("close", onClose)
    if (!dialog.open) dialog.showModal()
    return () => dialog.removeEventListener("close", onClose)
  }, [onClose])

  return (
    <dialog
      className="research-source-panel"
      ref={ref}
      aria-labelledby={titleId}
      onClick={(event) => {
        if (event.target === event.currentTarget) ref.current?.close()
      }}
    >
      <div className="research-source-content">
        <header className="research-source-header">
          <h2 id={titleId}>{metadata?.title ?? "Source evidence"}</h2>
          <button
            type="button"
            className="research-source-close"
            aria-label="Close source"
            onClick={() => ref.current?.close()}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        {url ? (
          <a
            className="research-source-url"
            href={url}
            target="_blank"
            rel="noreferrer"
          >
            {url}
          </a>
        ) : null}
        {metadata?.fetched_at ? (
          <p className="research-source-date">
            Retrieved{" "}
            <time dateTime={metadata.fetched_at}>
              {new Date(metadata.fetched_at).toLocaleString()}
            </time>
          </p>
        ) : null}
        {source.isPending ? <p role="status">Loading saved evidence…</p> : null}
        {source.error ? (
          <div role="alert">
            <p>
              {source.error instanceof ApiError && source.error.status === 404
                ? "This source is unavailable or has been cleared."
                : "Saved evidence could not be loaded."}
            </p>
            <button type="button" onClick={() => void source.refetch()}>
              Retry
            </button>
          </div>
        ) : null}
        {unavailable ? (
          <p role="status">
            This source is {metadata.status.replaceAll("_", " ")}. Saved
            evidence is unavailable.
          </p>
        ) : source.data && !source.error ? (
          <>
            <p className="research-source-range">
              Cited lines {citation.start_line}–{citation.end_line}
            </p>
            <ol
              className="research-source-lines"
              start={source.data.start_line}
            >
              {source.data.lines.map((line, index) => (
                <li key={source.data!.start_line + index}>{line || " "}</li>
              ))}
            </ol>
            {citation.end_line - citation.start_line + 1 > 200 ? (
              <p>
                Showing the first 200 cited lines. Open the original source for
                the full text.
              </p>
            ) : null}
          </>
        ) : null}
      </div>
    </dialog>
  )
}
