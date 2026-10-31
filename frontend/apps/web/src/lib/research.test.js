import assert from "node:assert/strict"
import { test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { MessageMarkdown } from "../components/message-markdown"
import { RunActivity } from "../components/run-activity"
import { RunActivityDisclosure } from "../components/run-activity-disclosure"
import { SavedRunActivity } from "../components/saved-run-activity"
import { describeActivity } from "./activity-status"
import { threadMessages } from "./assistant-outputs"
import {
  citationKey,
  citationTokens,
  transformCitationTree,
} from "./web-evidence"
import { WebEvidenceContext } from "./web-evidence-context"
import { webEvidenceKeys } from "./web-evidence-queries"
import { groupRunEvents } from "./run-event-groups"
import { runInfoFromEvent, turnFailureAction } from "./run-recovery"
import { createApiClient } from "./api"
import { researchQueryOptions } from "./research"
import { runEventsQueryOptions } from "./server-queries"

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

const researchOutcome = (phase = "partial") => ({
  run_id: "research-run",
  phase,
  objective: "Compare opportunities",
  subquestions: [],
  tasks: [],
  gaps: [],
  conflicts: [],
  pending_clarification: null,
  remaining_seconds: null,
  stop_reason:
    phase === "partial" ? "report_validation_failed" : "coverage_sufficient",
  citations: [],
})

const completedSummary = (mode = "research") => ({
  run_id: "research-run",
  turn_id: "research-turn",
  status: "completed",
  run_mode: mode,
  last_sequence: 2,
  created_at: "2026-10-31T10:00:00Z",
  finished_at: "2026-10-31T10:05:17Z",
  stop_reason: "report_validation_failed",
})

test("collapsed completed research shows the saved partial outcome", () => {
  const client = new QueryClient()
  client.setQueryData(
    researchQueryOptions("session", "research-run").queryKey,
    researchOutcome()
  )
  const markup = renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client },
      createElement(RunActivityDisclosure, {
        sessionId: "session",
        turnId: "research-turn",
        activity: { events: [], summary: completedSummary() },
        pending: false,
        expanded: false,
        onToggle: () => {},
        onClose: () => {},
        approvalPendingToolId: null,
        approvalError: null,
        onApprovalDecision: () => {},
      })
    )
  )
  assert.match(markup, /Research finished with gaps/)
  assert.doesNotMatch(markup, />Completed</)
})

test("saved activity restores research outcome after history reload", () => {
  const client = new QueryClient()
  client.setQueryData(
    researchQueryOptions("session", "research-run").queryKey,
    researchOutcome()
  )
  client.setQueryData(
    runEventsQueryOptions("session", "research-run").queryKey,
    { pages: [{ items: [] }], pageParams: [0] }
  )
  const markup = renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client },
      createElement(SavedRunActivity, {
        sessionId: "session",
        runId: "research-run",
        runInfo: null,
        summary: completedSummary(),
      })
    )
  )
  assert.match(markup, /Research finished with gaps/)
  assert.doesNotMatch(markup, />Completed</)
})

test("live research completion labels distinguish accepted and partial reports", () => {
  const render = (mode, research) =>
    renderToStaticMarkup(
      createElement(RunActivity, {
        events: [event(1, "run.started"), event(2, "run.completed")],
        runInfo: { runId: "research-run", runMode: mode, status: "completed" },
        research,
        pending: false,
        approvalPendingToolId: null,
        approvalError: null,
        onApprovalDecision: () => {},
      })
    )
  assert.match(
    render("research", researchOutcome()),
    /Research finished with gaps/
  )
  assert.doesNotMatch(render("research", researchOutcome()), /Run completed/)
  assert.match(
    render("research", researchOutcome("completed")),
    /Research completed/
  )
  assert.match(render("chat"), />Completed</)
  assert.match(render("chat"), /Run completed/)
  assert.match(
    render("research", { ...researchOutcome(), run_id: "other-run" }),
    /Research finished</
  )
})

test("unavailable research before admission lets the user change mode and retry", () => {
  assert.equal(turnFailureAction("research_unavailable", null, false), "retry")
})

test("citations preserve code and transform evidence references in plain text", () => {
  const source = `Evidence ${marker}.`
  const tree = {
    type: "root",
    children: [
      { type: "code", value: marker },
      { type: "inlineCode", value: marker },
      {
        type: "paragraph",
        children: [
          {
            type: "text",
            value: source,
            position: {
              start: { offset: 0 },
              end: { offset: source.length },
            },
          },
        ],
      },
    ],
  }
  const references = transformCitationTree(tree, source)
  assert.equal(tree.children[0].value, marker)
  assert.equal(tree.children[1].value, marker)
  const citation = tree.children[2].children?.[1]
  assert.equal(citation.type, "trellisCitation")
  assert.equal(citation.data.hProperties["data-citation"], `${sourceId}:12:14`)
  assert.deepEqual(references, [
    { source_id: sourceId, start_line: 12, end_line: 14 },
  ])
})

test("source binding requires the exact source and line range", () => {
  const citation = { source_id: sourceId, start_line: 12, end_line: 14 }
  assert.equal(citationKey(citation), citationKey({ ...citation }))
  assert.notEqual(
    citationKey(citation),
    citationKey({ ...citation, end_line: 15 })
  )
  assert.notEqual(
    citationKey(citation),
    citationKey({ ...citation, source_id: "other" })
  )
  assert.equal(
    citationTokens(`[[source:${sourceId}:L14-L12]]`)[0].reference,
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
  assert.equal(result[0].modelCallId, "checked-report-call")
  assert.equal(result[0].finalOutput, true)
  assert.equal(result[0].id, message.id)
})

test("ordinary chat keeps working notes unverified and published answer identity", () => {
  const output = {
    id: "chat-output",
    turn_id: "chat-turn",
    run_id: "chat-run",
    model_call_id: "chat-call",
    step_index: 1,
    content: `Answer ${marker}`,
    status: "complete",
    stop_reason: null,
    promoted_message_id: null,
    created_at: "2026-10-31T10:00:00Z",
  }
  const note = threadMessages([], [output])[0]
  assert.equal(note.modelCallId, "chat-call")
  assert.equal(note.finalOutput, false)
  const message = {
    id: "chat-message",
    turn_id: output.turn_id,
    role: "assistant",
    content: output.content,
    provider: "provider",
    model: "model",
    created_at: output.created_at,
  }
  const answer = threadMessages(
    [message],
    [{ ...output, promoted_message_id: message.id }]
  )[0]
  assert.equal(answer.id, message.id)
  assert.equal(answer.modelCallId, "chat-call")
  assert.equal(answer.finalOutput, true)
  assert.equal(answer.output, null)
})

test("published reports render only bindings for their own model call and exact range", () => {
  const modelCallId = "checked-report-call"
  const binding = {
    model_call_id: modelCallId,
    source_id: sourceId,
    start_line: 12,
    end_line: 14,
    citation_index: 1,
  }
  const render = (items, citationState = "final") => {
    const client = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity } },
    })
    client.setQueryData(
      webEvidenceKeys.citations("owner", "session", modelCallId),
      { items }
    )
    return renderToStaticMarkup(
      createElement(
        QueryClientProvider,
        { client },
        createElement(
          WebEvidenceContext.Provider,
          {
            value: {
              ownerId: "owner",
              sessionId: "session",
              revision: 0,
              openSource: () => {},
            },
          },
          createElement(MessageMarkdown, {
            content: marker,
            modelCallId,
            citationState,
          })
        )
      )
    )
  }
  assert.match(render([binding]), /aria-label="Source 1, lines 12 to 14"/)
  assert.match(
    render([{ ...binding, model_call_id: "another-call" }]),
    /Citation unavailable/
  )
  assert.match(render([{ ...binding, end_line: 15 }]), /Citation unavailable/)
  assert.match(render([binding], "streaming"), /Citation pending/)
  assert.match(render([binding], "unverified"), /Citation unavailable/)
  assert.match(render([binding], "literal"), /\[\[source:/)
})

test("activity preserves research phases alongside detailed web progress", () => {
  const markup = renderToStaticMarkup(
    createElement(RunActivity, {
      events: [
        event(1, "run.phase", { phase: "reviewing" }),
        event(2, "run.phase", {
          phase: "web_crawl",
          tool_call_id: "crawl",
          status: "page_completed",
          pages_completed: 2,
          max_pages: 4,
        }),
        event(3, "research.clarification_requested", {
          question: "Which region?",
        }),
        event(4, "tool.call", { tool_call_id: "read", name: "web.read" }),
        event(5, "tool.result", {
          tool_call_id: "read",
          status: "completed",
          content: '{"title":"Saved source"}',
        }),
      ],
      runInfo: null,
      pending: true,
      approvalPendingToolId: null,
      approvalError: null,
      onApprovalDecision: () => {},
    })
  )
  assert.match(markup, /Checking cited claims/)
  assert.match(markup, /Collecting site pages/)
  assert.match(markup, /2 of 4 pages processed/)
  assert.match(markup, /Clarification requested · Timer paused/)
  assert.match(markup, /aria-label="Expand web.read result"/)
  assert.equal(
    describeActivity(
      [event(1, "run.phase", { phase: "web_crawl" })],
      true,
      Date.now()
    ).label,
    "Collecting site pages"
  )
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
  await client.readWebSource("my session", sourceId, 12, 200)
  await client.getResearch("my session", "research/run")
  await client.readWebCitations("my session", "report/call")
  assert.deepEqual(requests, [
    {
      url: `https://trellis.test/api/sessions/my%20session/sources/${sourceId}?start_line=12&max_lines=200`,
      authorization: "Bearer test-token",
    },
    {
      url: "https://trellis.test/api/sessions/my%20session/runs/research%2Frun/research",
      authorization: "Bearer test-token",
    },
    {
      url: "https://trellis.test/api/sessions/my%20session/citations/report%2Fcall",
      authorization: "Bearer test-token",
    },
  ])
})
