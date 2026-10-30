import { queryOptions } from "@tanstack/react-query"

import { api } from "@/lib/api"
import type { SourceCitation } from "@/lib/app-types"

export function researchQueryOptions(sessionId: string, runId: string) {
  return queryOptions({
    queryKey: ["research", sessionId, runId],
    queryFn: ({ signal }) => api.getResearch(sessionId, runId, { signal }),
    retry: false,
    staleTime: 0,
  })
}

const sourceMarker = /\[\[source:([a-f\d-]{36}):L([1-9]\d*)-L([1-9]\d*)\]\]/gi

export function reportCitations(content: string): SourceCitation[] {
  return [...content.matchAll(sourceMarker)].map((match) => ({
    source_id: match[1],
    start_line: Number(match[2]),
    end_line: Number(match[3]),
  }))
}

type MarkdownNode = {
  type: string
  value?: string
  url?: string
  children?: MarkdownNode[]
}

export function remarkSourceCitations() {
  return (tree: MarkdownNode) => {
    let index = 0
    const visit = (parent: MarkdownNode) => {
      if (!parent.children || parent.type === "link") return
      parent.children = parent.children.flatMap((node) => {
        if (node.type !== "text" || !node.value) {
          visit(node)
          return [node]
        }
        const replacement: MarkdownNode[] = []
        let cursor = 0
        for (const match of node.value.matchAll(sourceMarker)) {
          if (match.index > cursor)
            replacement.push({
              type: "text",
              value: node.value.slice(cursor, match.index),
            })
          index += 1
          replacement.push({
            type: "link",
            url: `#trellis-citation-${index}-${match[1]}-${match[2]}-${match[3]}`,
            children: [{ type: "text", value: String(index) }],
          })
          cursor = match.index + match[0].length
        }
        if (!cursor) return [node]
        if (cursor < node.value.length)
          replacement.push({ type: "text", value: node.value.slice(cursor) })
        return replacement
      })
    }
    visit(tree)
  }
}

export function citationFromHref(href: string | undefined) {
  const match = href?.match(
    /^#trellis-citation-([1-9]\d*)-([a-f\d-]{36})-([1-9]\d*)-([1-9]\d*)$/i
  )
  return match
    ? {
        index: match[1],
        citation: {
          source_id: match[2],
          start_line: Number(match[3]),
          end_line: Number(match[4]),
        },
      }
    : null
}

export function sameCitation(left: SourceCitation, right: SourceCitation) {
  return (
    left.source_id === right.source_id &&
    left.start_line === right.start_line &&
    left.end_line === right.end_line
  )
}

export function researchPhaseLabel(phase: string): string {
  const labels: Record<string, string> = {
    answering: "Answering",
    tool_use: "Using tools",
    waiting: "Waiting",
    finishing: "Finishing",
    compacting: "Compacting conversation",
    web_search: "Searching sources",
    web_read: "Reading evidence",
    scoping: "Understanding the question",
    planning: "Planning the research",
    researching: "Researching subquestions",
    collecting: "Gathering evidence",
    comparing: "Comparing sources",
    checking_conflicts: "Checking conflicting claims",
    checking_conflict: "Checking conflicting claims",
    writing: "Preparing the report",
    drafting: "Preparing the report",
    reviewing: "Checking cited claims",
    checking_citations: "Checking cited claims",
    repairing: "Revising the report",
    waiting_for_input: "Waiting for your clarification",
    completed: "Research completed",
    partial: "Research finished with gaps",
    failed: "Research stopped",
    stopped: "Research stopped",
    interrupted: "Research interrupted",
    cancelled: "Research cancelled",
  }
  return labels[phase] ?? "Continuing"
}
