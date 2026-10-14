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
