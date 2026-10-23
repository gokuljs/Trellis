import assert from "node:assert/strict"
import { test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import type { User } from "@supabase/supabase-js"

import { WorkspaceRecoveryNotice } from "../src/components/workspace-review"
import { createApiClient } from "../src/lib/api"
import type { LatestRun } from "../src/lib/app-types"
import {
  allowsRunRetry,
  mergeRunInfo,
  recoveryRunInfo,
  runInfoFromEvent,
} from "../src/lib/run-recovery"
import type { RuntimeRunEvent } from "../src/lib/runtime-client"

const uncertain: LatestRun = {
  run_id: "run-original",
  turn_id: "turn-original",
  status: "interrupted",
  last_sequence: 42,
  retry_allowed: false,
  recovery_reason: "command_outcome_unknown",
  uncertain_tool_call_id: "tool-command",
  uncertain_operation: {
    tool_call_id: "tool-command",
    name: "run_command",
    arguments: { command: "make release", cwd: "workspace" },
    reviewed_at: null,
  },
}

function event(data: Record<string, unknown>): RuntimeRunEvent {
  return {
    runId: uncertain.run_id,
    sequence: 42,
    eventType: "run.interrupted",
    eventVersion: 1,
    data,
  }
}

test("a live unknown-outcome event blocks Retry before saved details refresh", () => {
  const info = runInfoFromEvent(
    { runId: uncertain.run_id, retryAllowed: true },
    event({
      retry_allowed: false,
      recovery_reason: "command_outcome_unknown",
      uncertain_tool_call_id: "tool-command",
    })
  )
  assert.deepEqual(info, {
    runId: uncertain.run_id,
    status: "interrupted",
    retryAllowed: false,
    recoveryReason: "command_outcome_unknown",
    uncertainToolCallId: "tool-command",
  })
  const activity = { events: [], runInfo: info }
  assert.equal(allowsRunRetry(activity), false)
  const stale = mergeRunInfo(info, {
    runId: uncertain.run_id,
    retryAllowed: true,
  })
  assert.equal(allowsRunRetry({ ...activity, runInfo: stale }), false)
  assert.equal(
    allowsRunRetry(
      { events: [], runInfo: { runId: "new-attempt", retryAllowed: true } },
      uncertain
    ),
    true
  )
  assert.equal(allowsRunRetry(undefined, uncertain), false)
})

test("canonical model policy still allows an explicit new attempt", () => {
  const model = {
    ...uncertain,
    retry_allowed: true,
    uncertain_tool_call_id: null,
    uncertain_operation: null,
    recovery_reason: "model_outcome_unknown",
  }
  assert.equal(
    allowsRunRetry({ events: [], runInfo: recoveryRunInfo(model) }, model),
    true
  )
  assert.equal(
    allowsRunRetry({
      events: [event({ retry_allowed: false })],
      runInfo: { runId: uncertain.run_id },
    }),
    false
  )
})

test("command review requires explicit confirmation and retains honest unknown outcome text", () => {
  const html = renderToStaticMarkup(
    createElement(WorkspaceRecoveryNotice, {
      run: uncertain,
      onReview: async () => undefined,
    })
  )
  assert.ok(html.includes("Command outcome unknown"))
  assert.ok(html.includes("make release"))
  assert.ok(html.includes("tool-command"))
  assert.ok(html.includes('type="checkbox"'))
  assert.ok(html.includes('type="submit" disabled=""'))
  assert.ok(
    html.includes("does not repeat the command or confirm that it succeeded")
  )
  const acknowledged = renderToStaticMarkup(
    createElement(WorkspaceRecoveryNotice, {
      run: {
        ...uncertain,
        uncertain_operation: {
          ...uncertain.uncertain_operation!,
          reviewed_at: "2026-10-23T00:00:00Z",
          review_pending: false,
        },
      },
      onReview: async () => undefined,
    })
  )
  assert.ok(acknowledged.includes("Review acknowledged"))
  assert.ok(acknowledged.includes("unknown outcome and cannot be retried"))
  assert.ok(!acknowledged.includes('type="submit"'))
})

test("patch uncertainty explains blocked Retry without a command acknowledgment form", () => {
  const html = renderToStaticMarkup(
    createElement(WorkspaceRecoveryNotice, {
      run: {
        ...uncertain,
        uncertain_operation: {
          ...uncertain.uncertain_operation!,
          name: "apply_patch",
        },
      },
      onReview: async () => undefined,
    })
  )
  assert.ok(html.includes("Retry is unavailable"))
  assert.ok(!html.includes('type="checkbox"'))
})

test("workspace acknowledgment posts the exact run and tool identity once", async () => {
  const requests: { url: unknown; init?: RequestInit }[] = []
  const credential = {
    token: "private-test-token",
    userId: "account",
    sessionEpoch: 1,
  }
  const client = createApiClient({
    resolveUrl: (path) => path,
    waitUntilReady: async () => undefined,
    getAuthController: () => ({
      getSnapshot: () => ({
        status: "signed-in",
        user: { id: "account" } as User,
        sessionEpoch: 1,
        error: null,
        pendingProvider: null,
      }),
      getAccessToken: async () => credential,
      isCredentialCurrent: () => true,
      expireSession: async () => undefined,
    }),
    fetch: async (url, init) => {
      requests.push({ url, init })
      return Response.json({
        tool_call_id: "tool-command",
        reviewed_at: "2026-10-23T00:00:00Z",
      })
    },
  })
  assert.deepEqual(
    await client.reviewWorkspace(
      "chat with spaces",
      uncertain.run_id,
      "tool-command"
    ),
    { tool_call_id: "tool-command", reviewed_at: "2026-10-23T00:00:00Z" }
  )
  assert.equal(requests.length, 1)
  assert.equal(
    requests[0].url,
    "/api/sessions/chat%20with%20spaces/workspace/review"
  )
  assert.equal(requests[0].init?.method, "POST")
  assert.deepEqual(JSON.parse(String(requests[0].init?.body)), {
    run_id: uncertain.run_id,
    tool_call_id: "tool-command",
    confirmed: true,
  })
})

test("a saved review timestamp cannot hide an uncleared local block", () => {
  const run = {
    ...uncertain,
    uncertain_operation: {
      ...uncertain.uncertain_operation!,
      reviewed_at: "2026-10-23T00:00:00Z",
      review_pending: true,
    },
  }
  const html = renderToStaticMarkup(
    createElement(WorkspaceRecoveryNotice, {
      run,
      onReview: async () => undefined,
    })
  )
  assert.ok(html.includes('type="submit" disabled=""'))
  assert.ok(!html.includes("workspace block is cleared"))
})

test("workspace markers keep an older command reviewable independently of the latest run", () => {
  const html = renderToStaticMarkup(
    createElement(WorkspaceRecoveryNotice, {
      run: {
        run_id: "new-run",
        turn_id: "new-turn",
        status: "completed",
        last_sequence: 1,
        retry_allowed: true,
      },
      workspace: {
        blocked: true,
        source_session_id: "original-chat",
        workspace_path: "/trusted/original-workspace",
        run_id: uncertain.run_id,
        uncertain_operation: uncertain.uncertain_operation,
      },
      onReview: async () => undefined,
    })
  )
  assert.ok(html.includes("tool-command"))
  assert.ok(html.includes("/trusted/original-workspace"))
  assert.ok(html.includes("Acknowledge review"))
  const privateBlock = renderToStaticMarkup(
    createElement(WorkspaceRecoveryNotice, {
      run: null,
      workspace: { blocked: true, reason: "workspace_review_other_account" },
      onReview: async () => undefined,
    })
  )
  assert.ok(privateBlock.includes("another account"))
  assert.ok(!privateBlock.includes("make release"))
  assert.ok(!privateBlock.includes('type="checkbox"'))
})
