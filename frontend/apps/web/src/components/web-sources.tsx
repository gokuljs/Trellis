import { BookOpen, Trash2, X } from "lucide-react"
import { useEffect, useId, useRef, useState, type ReactNode } from "react"
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import { api, ApiError } from "@/lib/api"
import { useAuth } from "@/lib/use-auth"
import { WebEvidenceContext, useWebEvidence } from "@/lib/web-evidence-context"
import {
  discardWebEvidence,
  webEvidenceKeys,
  webSourceLinesQueryOptions,
  webSourcesQueryOptions,
} from "@/lib/web-evidence-queries"
import {
  safePublicSourceUrl,
  sourceStatusLabel,
  type SourceSelection,
} from "@/lib/web-evidence"
import "./web-sources.css"

export function WebEvidenceScope({
  sessionId,
  active,
  children,
}: {
  sessionId: string
  active: boolean
  children: ReactNode
}) {
  const ownerId = useAuth().user?.id ?? ""
  return (
    <OwnedWebEvidenceScope
      key={`${ownerId}:${sessionId}`}
      ownerId={ownerId}
      sessionId={sessionId}
      active={active}
    >
      {children}
    </OwnedWebEvidenceScope>
  )
}

function OwnedWebEvidenceScope({
  ownerId,
  sessionId,
  active,
  children,
}: {
  ownerId: string
  sessionId: string
  active: boolean
  children: ReactNode
}) {
  const client = useQueryClient()
  const [revision, setRevision] = useState(0)
  const [expanded, setExpanded] = useState(false)
  const [selection, setSelection] = useState<SourceSelection | null>(null)
  const panelId = useId()
  const panel = useRef<HTMLElement>(null)
  useEffect(
    () => () => {
      void client.cancelQueries({
        queryKey: webEvidenceKeys.scope(ownerId, sessionId),
      })
    },
    [client, ownerId, sessionId]
  )
  const sources = useInfiniteQuery({
    ...webSourcesQueryOptions(ownerId, sessionId, revision),
    enabled: expanded && Boolean(ownerId),
  })
  const wasActive = useRef(active)
  useEffect(() => {
    if (wasActive.current && !active)
      void client.invalidateQueries({
        queryKey: webEvidenceKeys.sources(ownerId, sessionId, revision),
      })
    wasActive.current = active
  }, [active, client, ownerId, sessionId, revision])
  const clear = useMutation({
    mutationFn: () => api.clearWebSources(sessionId),
    onSuccess: async () => {
      await discardWebEvidence(client, ownerId, sessionId)
      setRevision((value) => value + 1)
    },
  })
  const openSource = (next: SourceSelection) => {
    setSelection(next)
    setExpanded(true)
    requestAnimationFrame(() => {
      panel.current?.focus()
      panel.current?.scrollIntoView({ behavior: "instant", block: "nearest" })
    })
  }
  const items = [
    ...new Map(
      sources.data?.pages
        .flatMap((page) => page.items)
        .map((source) => [source.id, source]) ?? []
    ).values(),
  ]
  return (
    <WebEvidenceContext.Provider
      value={{ ownerId, sessionId, revision, openSource }}
    >
      <div className="web-sources-disclosure">
        <button
          type="button"
          className="web-sources-trigger"
          aria-expanded={expanded}
          aria-controls={expanded ? panelId : undefined}
          onClick={() => setExpanded((value) => !value)}
        >
          <BookOpen size={14} aria-hidden="true" /> Sources
        </button>
        {expanded ? (
          <section
            className="web-sources-panel"
            id={panelId}
            aria-label="Recorded web sources"
            tabIndex={-1}
            ref={panel}
          >
            <div className="web-sources-heading">
              <h2>Recorded web sources</h2>
              <button
                type="button"
                aria-label="Close sources"
                onClick={() => setExpanded(false)}
              >
                <X size={15} aria-hidden="true" />
              </button>
            </div>
            <div className="web-sources-clear">
              <button
                type="button"
                disabled={active || clear.isPending}
                onClick={() => clear.mutate()}
              >
                <Trash2 size={13} aria-hidden="true" />
                {clear.isPending ? "Clearing sources…" : "Clear sources"}
              </button>
              <span>
                Clears recorded source text and metadata. Existing messages and
                tool excerpts remain.
              </span>
              {active ? (
                <span>
                  Finish or stop the active run before clearing sources.
                </span>
              ) : null}
            </div>
            {clear.error ? (
              <p role="alert">
                {clear.error instanceof ApiError &&
                clear.error.code === "web_sources_busy"
                  ? "Finish or stop the active run before clearing sources."
                  : "Sources could not be cleared. Try again."}
              </p>
            ) : null}
            {clear.isSuccess ? (
              <p role="status">
                Sources cleared. Earlier citations will show that their sources
                are unavailable.
              </p>
            ) : null}
            {sources.isPending ? (
              <p role="status">Loading sources…</p>
            ) : sources.error ? (
              <p role="alert">
                Sources could not be loaded.{" "}
                <button type="button" onClick={() => void sources.refetch()}>
                  Retry sources
                </button>
              </p>
            ) : !items.length ? (
              <p>No recorded web sources in this chat.</p>
            ) : (
              <ul className="web-source-list">
                {items.map((source) => (
                  <li key={source.id}>
                    <button
                      type="button"
                      aria-pressed={selection?.source_id === source.id}
                      onClick={() =>
                        setSelection({
                          source_id: source.id,
                          start_line: 1,
                          end_line: source.line_count || 1,
                        })
                      }
                    >
                      <span>
                        {source.status === "cleared"
                          ? "Source cleared"
                          : source.title || "Untitled source"}
                      </span>
                      <small>
                        {sourceStatusLabel(source)}
                        {source.publisher ? ` · ${source.publisher}` : ""}
                      </small>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {sources.hasNextPage ? (
              <button
                type="button"
                disabled={sources.isFetchingNextPage}
                onClick={() => void sources.fetchNextPage()}
              >
                {sources.isFetchingNextPage
                  ? "Loading sources…"
                  : "Load more sources"}
              </button>
            ) : null}
            {selection ? (
              <SourceExcerpt
                key={`${selection.source_id}:${selection.start_line}:${selection.end_line}:${revision}`}
                selection={selection}
              />
            ) : null}
          </section>
        ) : null}
      </div>
      {children}
    </WebEvidenceContext.Provider>
  )
}

function SourceExcerpt({ selection }: { selection: SourceSelection }) {
  const evidence = useWebEvidence()!
  const [start, setStart] = useState(selection.start_line)
  const count = Math.min(200, selection.end_line - start + 1)
  const excerpt = useQuery(
    webSourceLinesQueryOptions(
      evidence.ownerId,
      evidence.sessionId,
      selection.source_id,
      start,
      count,
      evidence.revision
    )
  )
  const result = excerpt.data
  const source = result?.source
  const last = result ? result.start_line + result.lines.length - 1 : start
  const url = safePublicSourceUrl(source?.final_url)
  return (
    <section className="web-source-excerpt" aria-label="Source excerpt">
      {excerpt.isPending ? (
        <p role="status">Loading recorded lines…</p>
      ) : excerpt.error || !result || !source ? (
        <p role="alert">
          Source could not be loaded.{" "}
          <button type="button" onClick={() => void excerpt.refetch()}>
            Retry source
          </button>
        </p>
      ) : source.status === "cleared" ? (
        <p role="status">
          Source cleared. Its text and metadata are no longer available.
        </p>
      ) : (
        <>
          <h3>{source.title || "Untitled source"}</h3>
          <p>
            {sourceStatusLabel(source)}
            {source.error_code
              ? ` · ${source.error_code.replaceAll("_", " ")}`
              : ""}
          </p>
          {source.final_url ? (
            <p className="web-source-url">
              {url ? (
                <a href={url} target="_blank" rel="noreferrer noopener">
                  {source.final_url}
                </a>
              ) : (
                <span>{source.final_url}</span>
              )}
            </p>
          ) : null}
          <dl className="web-source-metadata">
            {source.publisher ? (
              <>
                <dt>Publisher</dt>
                <dd>{source.publisher}</dd>
              </>
            ) : null}
            {source.published_at ? (
              <>
                <dt>Published</dt>
                <dd>{source.published_at}</dd>
              </>
            ) : null}
            {source.fetched_at ? (
              <>
                <dt>Fetched</dt>
                <dd>{source.fetched_at}</dd>
              </>
            ) : null}
          </dl>
          {result.lines.length ? (
            <>
              <p className="web-source-range">
                Showing L{result.start_line}–L{last} of {result.total_lines}{" "}
                recorded lines
                {selection.end_line < result.total_lines
                  ? ` · requested L${selection.start_line}–L${selection.end_line}`
                  : ""}
              </p>
              <ol className="web-source-lines" start={result.start_line}>
                {result.lines.map((line, index) => (
                  <li key={result.start_line + index}>
                    <span>{line || " "}</span>
                  </li>
                ))}
              </ol>
              <div className="web-source-pagination">
                {start > selection.start_line ? (
                  <button
                    type="button"
                    onClick={() => setStart(selection.start_line)}
                  >
                    First requested lines
                  </button>
                ) : null}
                {last < Math.min(selection.end_line, result.total_lines) ? (
                  <button type="button" onClick={() => setStart(last + 1)}>
                    Load next lines
                  </button>
                ) : null}
              </div>
            </>
          ) : (
            <p>No recorded supporting lines are available.</p>
          )}
        </>
      )}
    </section>
  )
}
