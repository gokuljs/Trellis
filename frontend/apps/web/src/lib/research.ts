import { queryOptions } from "@tanstack/react-query"

import { api } from "@/lib/api"
import type { ResearchSnapshot } from "@/lib/app-types"

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

export function researchQuestionEvidenceLabel(
  research: ResearchSnapshot,
  questionId: string,
  fallback: string
): string {
  const requirements = research.coverage?.find(
    (question) => question.id === questionId
  )?.requirements
  if (!requirements?.length) return fallback.replaceAll("_", " ")
  const recorded = requirements.filter((item) =>
    ["evidence_recorded", "reviewed"].includes(item.status)
  ).length
  const reviewed = requirements.filter(
    (item) => item.status === "reviewed"
  ).length
  const areas = requirements.length === 1 ? "evidence area" : "evidence areas"
  if (reviewed === requirements.length) {
    return `${reviewed}/${requirements.length} ${areas} reviewed`
  }
  if (!recorded) return `${requirements.length} ${areas} missing`
  return `${recorded}/${requirements.length} ${areas} recorded${
    reviewed ? ` · ${reviewed} reviewed` : ""
  }`
}

export function researchEvidenceDepthLabel(depths: string[] = []): string {
  if (depths.length === 1 && depths[0] === "abstract") {
    return "Abstract-only evidence"
  }
  const labels: Record<string, string> = {
    source_excerpt: "Source excerpts",
    abstract: "Abstracts",
    full_text: "Full-text excerpts",
    listing: "Job listings",
  }
  return [...new Set(depths)]
    .map((depth) => labels[depth])
    .filter(Boolean)
    .join(" · ")
}

export function researchBudgetDescription(
  preset: "conservative" | "longer",
  executionBudgetSeconds?: number | null
): string {
  const extended = preset === "longer"
  const minutes = Math.round(
    (executionBudgetSeconds ?? (extended ? 2700 : 600)) / 60
  )
  return extended
    ? `Research limits: ${minutes} minutes, 600,000 tokens, $15 estimated model cost, 16 searches, 40 pages.`
    : `Research limits: ${minutes} minutes, 300,000 tokens, $6 estimated model cost, 8 searches, 20 pages.`
}
