import assert from "node:assert/strict"
import { test } from "bun:test"

import { describeActivity } from "./activity-status"
import { threadMessages } from "./assistant-outputs"
import {
  citationFromHref,
  remarkSourceCitations,
  sameCitation,
} from "./research"
import { groupRunEvents } from "./run-event-groups"
import { runInfoFromEvent, turnFailureAction } from "./run-recovery"
import { createApiClient } from "./api"

const sourceId = "29b96351-a37d-44b2-ae6e-1ccdbd210b4c"
const marker = `[[source:${sourceId}:L12-L14]]`
const event = (sequence, eventType, data = {}) => ({
  runId: "research-run",
  sequence,
  eventType,
  eventVersion: 1,
  data,
  createdAt: "2026-10-31T10:00:00Z",
})

test("unavailable research before admission lets the user change mode and retry", () => {
  assert.equal(turnFailureAction("research_unavailable", null, false), "retry")
})

test("citations preserve code and turn evidence references into numbered links", () => {
  const tree = {
    type: "root",
    children: [
      { type: "code", value: marker },
      { type: "inlineCode", value: marker },
      {
        type: "paragraph",
        children: [{ type: "text", value: `Evidence ${marker}.` }],
      },
    ],
  }
  remarkSourceCitations()(tree)
  assert.equal(tree.children[0].value, marker)
  assert.equal(tree.children[1].value, marker)
  const link = tree.children[2].children?.[1]
  assert.equal(link.type, "link")
  assert.deepEqual(citationFromHref(link.url), {
    index: "1",
    citation: { source_id: sourceId, start_line: 12, end_line: 14 },
  })
})

test("source binding requires the exact source and line range", () => {
  const citation = { source_id: sourceId, start_line: 12, end_line: 14 }
  assert.ok(sameCitation(citation, { ...citation }))
  assert.equal(sameCitation(citation, { ...citation, end_line: 15 }), false)
  assert.equal(
    sameCitation(citation, { ...citation, source_id: "other" }),
    false
  )
  assert.equal(
    citationFromHref("https://example.com/#trellis-citation-1"),
    null
  )
})

test("clarification pauses the visible timer and resumes the same research run", () => {
  const question = event(2, "research.clarification_requested")
  const info = runInfoFromEvent(
    { runId: "research-run", runMode: "research" },
    question
  )
  assert.equal(info.status, "waiting_for_input")
  assert.equal(info.runMode, "research")
  const status = describeActivity(
    [event(1, "run.started"), question, event(3, "model.usage")],
    true,
    Date.parse("2026-10-31T18:00:00Z")
  )
  assert.equal(status.label, "Waiting for your clarification")
  assert.equal(status.duration, null)
  assert.equal(status.terminal, false)
  const resumed = runInfoFromEvent(
    info,
    event(4, "research.clarification_answered")
  )
  assert.equal(resumed.status, "running")
  assert.equal(resumed.runId, "research-run")
})

test("parallel model usage remains attached to its own call", () => {
  const groups = groupRunEvents([
    event(1, "model.usage", { model_call_id: "worker-a" }),
    event(2, "model.usage", { model_call_id: "worker-b" }),
    event(3, "model.completed", { model_call_id: "worker-a" }),
    event(4, "model.completed", { model_call_id: "worker-b" }),
  ])
  assert.equal(groups.length, 2)
  assert.deepEqual(
    groups[0].events.map((item) => item.sequence),
    [1, 3]
  )
  assert.deepEqual(
    groups[1].events.map((item) => item.sequence),
    [2, 4]
  )
})

test("promoted reports keep citation identity without changing timeline order", () => {
  const message = {
    id: "report-message",
    turn_id: "turn",
    role: "assistant",
    content: `Report ${marker}`,
    provider: "provider",
    model: "model",
    created_at: "2026-10-31T10:00:00Z",
  }
  const output = {
    id: "report-output",
    turn_id: "turn",
    run_id: "research-run",
    model_call_id: "checked-report-call",
    step_index: 10,
    content: message.content,
    status: "complete",
    stop_reason: null,
    promoted_message_id: message.id,
    created_at: message.created_at,
  }
  const result = threadMessages([message], [output], {}, [
    { kind: "message", id: message.id },
  ])
  assert.equal(result.length, 1)
  assert.equal(result[0].output, null)
  assert.equal(result[0].reportOutput?.model_call_id, "checked-report-call")
  assert.equal(result[0].id, message.id)
})

test("source reads and research snapshots use authenticated, session-scoped requests", async () => {
  const requests = []
  const credential = { userId: "owner", sessionEpoch: 1, token: "test-token" }
  const client = createApiClient({
    getAuthController: () => ({
      getSnapshot: () => ({
        status: "signed-in",
        user: { id: "owner" },
        sessionEpoch: 1,
      }),
      getAccessToken: async () => credential,
      isCredentialCurrent: (current) => current === credential,
      expireSession: async () => {},
    }),
    waitUntilReady: async () => {},
    resolveUrl: (path) => `https://trellis.test${path}`,
    fetch: async (url, init) => {
      requests.push({ url, authorization: init.headers.get("Authorization") })
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    },
  })
  await client.readSource("my session", sourceId, 12, 300)
  await client.getResearch("my session", "research/run")
  assert.deepEqual(requests, [
    {
      url: `https://trellis.test/api/sessions/my%20session/sources/${sourceId}?start_line=12&max_lines=200`,
      authorization: "Bearer test-token",
    },
    {
      url: "https://trellis.test/api/sessions/my%20session/runs/research%2Frun/research",
      authorization: "Bearer test-token",
    },
  ])
})
