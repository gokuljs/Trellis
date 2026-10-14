import { expect, test } from "bun:test"

async function helpers() {
  return import("../apps/web/src/lib/run-recovery.ts")
}

test("exhausted transport retries require discovery even before a run acknowledgement", async () => {
  const { turnFailureAction } = await helpers()
  expect(turnFailureAction("connection_lost", null)).toBe("reconnect")
  expect(turnFailureAction("runtime_timeout", null)).toBe("reconnect")
})

test("a known completed run offers transcript reload instead of a second attempt", async () => {
  const { turnFailureAction } = await helpers()
  expect(turnFailureAction("service_unavailable", "completed")).toBe("reload")
})

test("a confirmed terminal failure remains retryable even if its code resembles a transport error", async () => {
  const { turnFailureAction } = await helpers()
  expect(turnFailureAction("connection_lost", "failed")).toBe("retry")
  expect(turnFailureAction("user_cancelled", "cancelled")).toBe("retry")
  expect(turnFailureAction("provider_not_configured", null)).toBe("retry")
})

test("recovery can discover the accepted turn when the session refresh is unavailable", async () => {
  const { recoveryTranscript } = await helpers()
  const previous = [
    {
      id: "old",
      turn_id: "old-turn",
      role: "assistant",
      content: "Saved answer",
      created_at: "2026-10-15T08:00:00Z",
    },
  ]
  const transcript = recoveryTranscript(previous, {
    turnId: "new-turn",
    content: "My accepted question",
  })
  expect(transcript.at(-1).turn_id).toBe("new-turn")
  expect(transcript.at(-1).content).toBe("My accepted question")
  expect(previous).toHaveLength(1)
  expect(
    recoveryTranscript(transcript, {
      turnId: "new-turn",
      content: "My accepted question",
    })
  ).toEqual(transcript)
})

test("run ownership rejects old callbacks after changing chats or starting another run", async () => {
  const { ownsRun } = await helpers()
  const owner = { sessionId: "chat-a", generation: 4 }
  expect(ownsRun(owner, { sessionId: "chat-a", generation: 4 })).toBe(true)
  expect(ownsRun(owner, { sessionId: "chat-b", generation: 4 })).toBe(false)
  expect(ownsRun(owner, { sessionId: "chat-a", generation: 5 })).toBe(false)
  expect(ownsRun(owner, { sessionId: null, generation: 4 })).toBe(false)
})

test("returning to the same chat never gives an older run permission to clear pending or replace its run ID", async () => {
  const { ownsRun } = await helpers()
  const oldOwner = { sessionId: "chat-a", generation: 4 }
  const currentOwner = { sessionId: "chat-a", generation: 7 }
  const view = { runId: "new-run", pending: true }
  const lateAcknowledgement = (runId) => {
    if (ownsRun(oldOwner, currentOwner)) view.runId = runId
  }
  const lateFinally = () => {
    if (ownsRun(oldOwner, currentOwner)) view.pending = false
  }
  lateAcknowledgement("old-run")
  lateFinally()
  expect(view).toEqual({ runId: "new-run", pending: true })
})
