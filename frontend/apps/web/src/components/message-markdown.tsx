import { useQuery } from "@tanstack/react-query"
import Markdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"

import { useWebEvidence } from "@/lib/web-evidence-context"
import { webCitationsQueryOptions } from "@/lib/web-evidence-queries"
import {
  citationKey,
  remarkCitations,
  safePublicSourceUrl,
} from "@/lib/web-evidence"

function Citation({
  markerKey,
  modelCallId,
  state,
}: {
  markerKey: string
  modelCallId?: string | null
  state: "streaming" | "final" | "unverified"
}) {
  const scope = useWebEvidence()
  const bindings = useQuery({
    ...webCitationsQueryOptions(
      scope?.ownerId ?? "",
      scope?.sessionId ?? "",
      modelCallId ?? "",
      scope?.revision
    ),
    enabled:
      state === "final" &&
      markerKey !== "invalid" &&
      Boolean(scope?.ownerId && modelCallId),
  })
  const binding = bindings.data?.items.find(
    (item) =>
      item.model_call_id === modelCallId &&
      Number.isSafeInteger(item.citation_index) &&
      item.citation_index >= 1 &&
      item.citation_index <= 50 &&
      citationKey(item) === markerKey
  )
  if (state === "streaming")
    return <span className="thread-citation pending">Citation pending</span>
  if (state === "final" && bindings.isFetching && !binding)
    return <span className="thread-citation pending">Checking source…</span>
  if (!binding || state !== "final" || !scope)
    return (
      <span
        className="thread-citation unavailable"
        title="This marker has no validated supporting source lines."
      >
        Citation unavailable
      </span>
    )
  return (
    <button
      className="thread-citation"
      type="button"
      aria-label={`Source ${binding.citation_index}, lines ${binding.start_line} to ${binding.end_line}`}
      onClick={() => scope.openSource(binding)}
    >
      [{binding.citation_index}]
    </button>
  )
}

export function MessageMarkdown({
  content,
  modelCallId = null,
  citationState = "unverified",
}: {
  content: string
  modelCallId?: string | null
  citationState?: "streaming" | "final" | "unverified" | "literal"
}) {
  const components: Components = {
    img({ src, alt }) {
      const safeSource = safePublicSourceUrl(src)
      return safeSource ? (
        <a href={safeSource} target="_blank" rel="noreferrer noopener">
          {alt || "Image"} (open image)
        </a>
      ) : (
        <span>{alt || "Image unavailable"}</span>
      )
    },
    span({ node, children, ...props }) {
      const markerKey = node?.properties["data-citation"]
      return typeof markerKey === "string" && citationState !== "literal" ? (
        <Citation
          markerKey={markerKey}
          modelCallId={modelCallId}
          state={citationState}
        />
      ) : (
        <span {...props}>{children}</span>
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
  return (
    <div className="thread-markdown">
      <Markdown
        remarkPlugins={
          citationState === "literal"
            ? [remarkGfm]
            : [remarkGfm, [remarkCitations, { source: content }]]
        }
        skipHtml
        components={components}
      >
        {content}
      </Markdown>
    </div>
  )
}
