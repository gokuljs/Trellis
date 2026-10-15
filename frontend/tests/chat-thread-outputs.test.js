import { expect, test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"

async function renderThread(overrides = {}) {
  const { ChatThread } =
    await import("../apps/web/src/components/chat-thread.tsx")
  return renderToStaticMarkup(
    createElement(ChatThread, {
      session: { id: "chat-a", title: "Saved chat", message_count: 1 },
      messages: [
        { id: "user-a", turn_id: "turn-a", role: "user", content: "Question" },
      ],
      pending: false,
      streamingText: null,
      error: null,
      canRetry: false,
      canCancel: false,
      cancellationPending: false,
      onRetry: () => {},
      onCancel: () => {},
      ...overrides,
    })
  )
}

function output(overrides = {}) {
  return {
    id: "output-a",
    turn_id: "turn-a",
    run_id: "run-a",
    model_call_id: "call-a",
    step_index: 1,
    content: "Preserved partial text",
    status: "partial",
    stop_reason: "user_cancelled",
    promoted_message_id: null,
    created_at: "2026-10-15T08:00:00Z",
    ...overrides,
  }
}

test("saved stopped output is visible with a partial label after generation ends", async () => {
  const markup = await renderThread({ assistantOutputs: [output()] })
  expect(markup).toContain("Preserved partial text")
  expect(markup).toContain("Partial answer · Stopped")
  expect(markup).not.toContain("Assistant response streaming")
})

test("live output uses the existing assistant message styling before completion", async () => {
  const markup = await renderThread({
    pending: true,
    assistantOutputs: [output({ status: "streaming", stop_reason: null })],
  })
  expect(markup).toContain("Preserved partial text")
  expect(markup).toContain('class="thread-message assistant pending"')
  expect(markup).toContain("Generating")
  expect(markup).not.toContain("Assistant response pending")
})

test("promoted final text is rendered once alongside a provider error", async () => {
  const markup = await renderThread({
    messages: [
      { id: "user-a", turn_id: "turn-a", role: "user", content: "Question" },
      {
        id: "message-a",
        turn_id: "turn-a",
        role: "assistant",
        content: "Saved final text",
      },
    ],
    assistantOutputs: [
      output({
        content: "Saved final text",
        status: "complete",
        promoted_message_id: "message-a",
      }),
    ],
    error: "Provider unavailable",
  })
  expect(markup.match(/Saved final text/g)).toHaveLength(1)
  expect(markup).toContain("Provider unavailable")
})

test("bounded history exposes older and latest controls and blocks paging during a run", async () => {
  const markup = await renderThread({
    hasOlderHistory: true,
    browsingHistory: true,
    historyLoading: false,
    pending: true,
  })
  expect(markup).toContain("Load older messages")
  expect(markup).toContain("Jump to latest")
  expect(markup).toContain('aria-label="Chat history"')
  expect(markup).toContain('disabled=""')
})

test("an older user turn can open saved attempts even when no assistant text was published", async () => {
  const markup = await renderThread()
  expect(markup).toContain("View activity")
  expect(markup).toContain('aria-expanded="false"')
})

test("a failed history reset keeps an explicit retry action without older page metadata", async () => {
  const markup = await renderThread({ historyError: "History is unavailable" })
  expect(markup).toContain("History is unavailable")
  expect(markup).toContain("Try again")
})

test("an active retry still shows its phase when the original user row is outside the history window", async () => {
  const markup = await renderThread({
    messages: [],
    pending: true,
    activeRunTurnId: "old-turn",
    runActivities: {
      "old-turn": {
        runInfo: { runId: "run" },
        events: [
          {
            runId: "run",
            sequence: 1,
            eventType: "run.phase",
            eventVersion: 1,
            data: { phase: "answering" },
          },
        ],
      },
    },
  })
  expect(markup).toContain("Answering")
})
