import { queryOptions } from "@tanstack/react-query"

import { api } from "@/lib/api"

export function researchQueryOptions(sessionId: string, runId: string) {
  return queryOptions({
    queryKey: ["research", sessionId, runId],
    queryFn: ({ signal }) => api.getResearch(sessionId, runId, { signal }),
    retry: false,
    staleTime: 0,
  })
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
