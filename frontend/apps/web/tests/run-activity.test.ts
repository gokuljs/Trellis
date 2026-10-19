import assert from "node:assert/strict"
import { test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { RunActivity } from "../src/components/run-activity"
import { SavedTurnActivity } from "../src/components/saved-turn-activity"
import { runSummariesQueryOptions } from "../src/lib/server-queries"
import { describeActivity } from "../src/lib/activity-status"
import {
  activityTotals,
  coalesceSummaryRefresh,
  newestRunSummary,
} from "../src/lib/activity-summary"
import type { RunSummary } from "../src/lib/app-types"
import type { RuntimeRunEvent } from "../src/lib/runtime-client"

function summary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: "saved-run-id",
    turn_id: "turn-id",
    retry_of: null,
    status: "failed",
    provider_id: "openai",
    model_id: "openai:gpt-5.5",
    budget_preset: "conservative",
    max_model_calls: 8,
    max_tool_calls: 16,
    max_total_tokens: 100_000,
    max_cost_usd: 2,
    deadline_at: "2026-10-16T00:10:00Z",
    last_sequence: 900,
    created_at: "2026-10-16T00:00:00Z",
    started_at: "2026-10-16T00:00:01Z",
    finished_at: "2026-10-16T00:00:10Z",
    model_call_count: 3,
    tool_call_count: 2,
    total_tokens: 1234,
    usage_complete: false,
    estimated_cost_usd: 0.012,
    cost_complete: false,
    stop_reason: "provider_unavailable",
    error_code: "provider_unavailable",
    error_message: "The model provider is temporarily unavailable.",
    ...overrides,
  }
}

function event(eventType: string, sequence = 901): RuntimeRunEvent {
  return {
    runId: "saved-run-id",
    sequence,
    eventType,
    eventVersion: 1,
    data: {},
    createdAt: "2026-10-16T00:00:05Z",
  }
}

test("run details survive an empty event history with honest partial totals", () => {
  const html = renderToStaticMarkup(
    createElement(RunActivity, {
      events: [],
      runInfo: null,
      summary: summary(),
      pending: false,
      approvalPendingToolId: null,
      approvalError: null,
      onApprovalDecision: () => undefined,
    })
  )
  for (const text of [
    "Failed",
    "Elapsed 10s",
    "3 / 8 model calls",
    "2 / 16 tools",
    "1,234 / 100,000 tokens (partial)",
    "$0.01 / $2.00 estimated (partial)",
    "Run saved-run-id",
    "Deadline",
    "The model provider is temporarily unavailable.",
    "Detailed activity is unavailable. Saved run details remain above.",
  ]) {
    assert.ok(html.includes(text), `missing saved detail: ${text}`)
  }
})

test("a cached older attempt summary cannot erase newer saved fallback details", () => {
  const client = new QueryClient()
  const options = runSummariesQueryOptions("chat", undefined, "turn-id")
  client.setQueryData(options.queryKey, {
    pages: [
      {
        items: [
          summary({
            last_sequence: 800,
            status: "running",
            model_call_count: 1,
            error_message: null,
            error_code: null,
          }),
        ],
        next_offset: null,
      },
    ],
    pageParams: [0],
  })
  try {
    const html = renderToStaticMarkup(
      createElement(
        QueryClientProvider,
        { client },
        createElement(SavedTurnActivity, {
          sessionId: "chat",
          turnId: "turn-id",
          fallback: summary(),
        })
      )
    )
    assert.ok(html.includes("3 / 8 model calls"))
    assert.ok(html.includes("The model provider is temporarily unavailable."))
    assert.ok(html.includes("Elapsed 10s"))
  } finally {
    client.clear()
  }
})

test("newer live progress suppresses stale saved failure text", () => {
  const html = renderToStaticMarkup(
    createElement(RunActivity, {
      events: [
        {
          ...event("run.phase"),
          data: { phase: "answering", model_call_id: "next-call" },
        },
      ],
      runInfo: null,
      summary: summary(),
      pending: true,
      approvalPendingToolId: null,
      approvalError: null,
      onApprovalDecision: () => undefined,
    })
  )
  assert.ok(html.includes("Answering"))
  assert.ok(!html.includes("The model provider is temporarily unavailable."))
})

test("failed summary loading preserves paged events and offers retry without invented model steps", () => {
  const html = renderToStaticMarkup(
    createElement(RunActivity, {
      events: [
        { ...event("model.completed"), data: { model_call_id: "sixth-call" } },
      ],
      runInfo: null,
      summaryError: "Run totals could not be loaded.",
      onRetrySummary: () => undefined,
      pending: true,
      approvalPendingToolId: null,
      approvalError: null,
      onApprovalDecision: () => undefined,
    })
  )
  assert.ok(html.includes("Run totals could not be loaded."))
  assert.ok(html.includes("Retry totals"))
  assert.ok(!html.includes("Run totals are loading…"))
  assert.ok(html.includes("Model call completed"))
  assert.ok(!html.includes("Model step 1"))
})

test("paged events never replace authoritative counts or turn unknown usage into zero", () => {
  const totals = activityTotals([event("model.completed", 899)], summary())
  assert.equal(totals.modelCalls, 3)
  assert.equal(totals.toolCalls, 2)
  assert.equal(totals.totalTokens, 1234)
  assert.equal(
    activityTotals([], summary({ total_tokens: null })).totalTokens,
    null
  )
  assert.equal(activityTotals([], summary({ total_tokens: 0 })).totalTokens, 0)
  assert.equal(activityTotals([], summary(), "another-run").modelCalls, null)
})

test("new progress marks saved totals provisional until its aggregate catches up", () => {
  const newer = event("model.usage")
  const complete = summary({ usage_complete: true, cost_complete: true })
  assert.equal(activityTotals([newer], complete).updating, true)
  assert.equal(activityTotals([newer], complete).usageComplete, false)
  assert.equal(activityTotals([newer], complete).costComplete, false)
  assert.equal(
    activityTotals([newer], { ...complete, last_sequence: 901 }).updating,
    false
  )
  assert.equal(
    newestRunSummary(complete.run_id, complete, {
      ...complete,
      last_sequence: 899,
    })?.last_sequence,
    900
  )
})

test("an old terminal summary cannot override newer live progress", () => {
  const progress = { ...event("run.phase"), data: { phase: "answering" } }
  const state = describeActivity(
    [progress],
    true,
    Date.parse("2026-10-16T00:00:12Z"),
    undefined,
    summary()
  )
  assert.equal(state.label, "Answering")
  assert.equal(state.terminal, false)
  assert.equal(state.duration, "12s")
})

test("summary refreshes coalesce persisted progress and ignore answer deltas", async () => {
  const refreshed: string[] = []
  const refresh = coalesceSummaryRefresh((sessionId) =>
    refreshed.push(sessionId)
  )
  for (let i = 0; i < 100; i++)
    refresh.schedule("chat", event("assistant.delta", i))
  await new Promise((resolve) => setTimeout(resolve, 1050))
  assert.deepEqual(refreshed, [])
  for (let i = 0; i < 100; i++)
    refresh.schedule("chat", event("model.usage", i))
  refresh.schedule("chat", event("tool.call"))
  await new Promise((resolve) => setTimeout(resolve, 1050))
  assert.deepEqual(refreshed, ["chat"])
  refresh.schedule("chat", event("run.failed"))
  refresh.dispose()
})
