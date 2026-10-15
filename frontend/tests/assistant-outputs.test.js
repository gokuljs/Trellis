import { expect, test } from "bun:test"

async function helpers() {
  return import("../apps/web/src/lib/assistant-outputs.ts")
}

function output(overrides = {}) {
  return {
    id: "output-a",
    turn_id: "turn-a",
    run_id: "run-a",
    model_call_id: "call-a",
    step_index: 1,
    content: "Hello world",
    status: "streaming",
    stop_reason: null,
    promoted_message_id: null,
    created_at: "2026-10-15T08:00:01Z",
    ...overrides,
  }
}

function event(sequence, eventType, data = {}, runId = "run-a") {
  return {
    runId,
    sequence,
    eventType,
    eventVersion: 1,
    createdAt: "2026-10-15T08:00:01Z",
    data,
  }
}

const userMessage = {
  id: "user-a",
  turn_id: "turn-a",
  role: "user",
  content: "Say hello",
  provider: null,
  model: null,
  created_at: "2026-10-15T08:00:00Z",
}

test("replay rebuilds a saved streaming output without doubling or shrinking its visible text", async () => {
  const {
    newAssistantOutputState,
    mergeSavedAssistantOutputs,
    reduceAssistantOutputEvent,
  } = await helpers()
  let state = mergeSavedAssistantOutputs(newAssistantOutputState(), [output()])
  state = reduceAssistantOutputEvent(
    state,
    event(1, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      batch_index: 0,
      text: "Hello",
    }),
    "turn-a"
  )
  expect(state.outputs[0].content).toBe("Hello world")
  state = reduceAssistantOutputEvent(
    state,
    event(2, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      batch_index: 1,
      text: " world!",
    }),
    "turn-a"
  )
  expect(state.outputs[0].content).toBe("Hello world!")
  const duplicate = reduceAssistantOutputEvent(
    state,
    event(2, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      batch_index: 1,
      text: " world!",
    }),
    "turn-a"
  )
  expect(duplicate.outputs[0].content).toBe("Hello world!")
})

test("provider failure preserves the published answer as partial even without a transcript refresh", async () => {
  const {
    newAssistantOutputState,
    reduceAssistantOutputEvent,
    outputStatusLabel,
  } = await helpers()
  let state = reduceAssistantOutputEvent(
    newAssistantOutputState(),
    event(1, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      batch_index: 0,
      text: "Useful partial text",
    }),
    "turn-a"
  )
  state = reduceAssistantOutputEvent(
    state,
    event(2, "run.failed", { code: "provider_error" }),
    "turn-a"
  )
  expect(state.outputs[0].content).toBe("Useful partial text")
  expect(state.outputs[0].status).toBe("partial")
  expect(outputStatusLabel(state.outputs[0])).toBe("Partial answer")
})

test("a promoted output appears once as the saved completed message", async () => {
  const { threadMessages } = await helpers()
  const completeMessage = {
    ...userMessage,
    id: "message-a",
    role: "assistant",
    content: "Hello world",
  }
  const entries = threadMessages(
    [userMessage, completeMessage],
    [
      output({
        status: "complete",
        promoted_message_id: "message-a",
      }),
    ]
  )
  expect(entries.map((entry) => entry.content)).toEqual([
    "Say hello",
    "Hello world",
  ])
  expect(entries[1].output).toBeNull()
})

test("text before a tool call completes as a working note while later text streams separately", async () => {
  const {
    newAssistantOutputState,
    reduceAssistantOutputEvent,
    outputStatusLabel,
  } = await helpers()
  let state = reduceAssistantOutputEvent(
    newAssistantOutputState(),
    event(1, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      step_index: 1,
      batch_index: 0,
      text: "Checking the file.",
    }),
    "turn-a"
  )
  state = reduceAssistantOutputEvent(
    state,
    event(2, "assistant.message", {
      output_id: "output-a",
      status: "complete",
      stop_reason: "tool_continuation",
      content: "Checking the file.",
    }),
    "turn-a"
  )
  state = reduceAssistantOutputEvent(
    state,
    event(3, "assistant.delta", {
      output_id: "output-b",
      model_call_id: "call-b",
      step_index: 2,
      batch_index: 0,
      text: "The file contains",
    }),
    "turn-a"
  )
  expect(state.outputs.map((item) => item.content)).toEqual([
    "Checking the file.",
    "The file contains",
  ])
  expect(outputStatusLabel(state.outputs[0])).toBe("Working note")
  expect(outputStatusLabel(state.outputs[1])).toBe("Generating")
})

test("completed output stays complete through terminal events and is reconciled with its saved message", async () => {
  const {
    newAssistantOutputState,
    reduceAssistantOutputEvent,
    mergeSavedAssistantOutputs,
    threadMessages,
  } = await helpers()
  let state = reduceAssistantOutputEvent(
    newAssistantOutputState(),
    event(1, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      step_index: 1,
      batch_index: 0,
      text: "Hello world",
    }),
    "turn-a"
  )
  state = reduceAssistantOutputEvent(
    state,
    event(2, "assistant.completed", {
      output_id: "output-a",
      model_call_id: "call-a",
      status: "complete",
      stop_reason: "final_response",
      promoted_message_id: "message-a",
      message_id: "message-a",
    }),
    "turn-a"
  )
  state = reduceAssistantOutputEvent(state, event(3, "run.completed"), "turn-a")
  expect(state.outputs[0].status).toBe("complete")
  expect(state.outputs[0].promoted_message_id).toBe("message-a")
  state = mergeSavedAssistantOutputs(state, [
    output({ status: "complete", promoted_message_id: "message-a" }),
  ])
  expect(
    threadMessages(
      [
        userMessage,
        {
          ...userMessage,
          id: "message-a",
          role: "assistant",
          content: "Hello world",
        },
      ],
      state.outputs
    )
  ).toHaveLength(2)
})

test("refresh keeps cancelled text and a stale streaming snapshot cannot reopen it", async () => {
  const {
    newAssistantOutputState,
    mergeSavedAssistantOutputs,
    outputStatusLabel,
  } = await helpers()
  let state = mergeSavedAssistantOutputs(newAssistantOutputState(), [
    output({ status: "partial", stop_reason: "user_cancelled" }),
  ])
  expect(outputStatusLabel(state.outputs[0])).toBe("Partial answer · Stopped")
  state = mergeSavedAssistantOutputs(state, [
    output({ content: "Hello different text" }),
  ])
  expect(state.outputs[0].status).toBe("partial")
  expect(state.outputs[0].content).toBe("Hello world")
})

test("retry attempts are ordered by run and keep a failed answer beside the successful retry", async () => {
  const { threadMessages } = await helpers()
  const completeMessage = {
    ...userMessage,
    id: "message-b",
    role: "assistant",
    content: "Finished answer",
  }
  const entries = threadMessages(
    [userMessage, completeMessage],
    [
      output({
        id: "output-b",
        run_id: "run-b",
        model_call_id: "call-b",
        status: "complete",
        content: "Finished answer",
        promoted_message_id: "message-b",
        created_at: "2026-10-15T08:02:00Z",
      }),
      output({
        status: "partial",
        stop_reason: "provider_error",
        content: "Failed answer",
      }),
    ]
  )
  expect(entries.map((item) => item.content)).toEqual([
    "Say hello",
    "Failed answer",
    "Finished answer",
  ])
  expect(entries[1].attemptNumber).toBe(1)
  expect(entries[2].attemptNumber).toBe(2)
})

test("replayed output batches are idempotent even when the same batch arrives with a later sequence", async () => {
  const { newAssistantOutputState, reduceAssistantOutputEvent } =
    await helpers()
  const data = {
    output_id: "output-a",
    model_call_id: "call-a",
    step_index: 1,
    batch_index: 0,
    text: "One copy",
  }
  let state = reduceAssistantOutputEvent(
    newAssistantOutputState(),
    event(1, "assistant.delta", data),
    "turn-a"
  )
  state = reduceAssistantOutputEvent(
    state,
    event(2, "assistant.delta", data),
    "turn-a"
  )
  expect(state.outputs[0].content).toBe("One copy")
  state = reduceAssistantOutputEvent(state, event(3, "run.cancelled"), "turn-a")
  state = reduceAssistantOutputEvent(
    state,
    event(4, "assistant.delta", {
      ...data,
      batch_index: 1,
      text: " late text",
    }),
    "turn-a"
  )
  expect(state.outputs[0].content).toBe("One copy")
  expect(state.outputs[0].status).toBe("partial")
})

test("a promotion link alone never hides an output while its saved message is unavailable", async () => {
  const { threadMessages } = await helpers()
  const entries = threadMessages(
    [userMessage],
    [output({ status: "complete", promoted_message_id: "missing-message" })]
  )
  expect(entries.map((item) => item.content)).toEqual([
    "Say hello",
    "Hello world",
  ])
})

test("a snapshot batch cursor seeds full text before a reconnect starts with its next delta", async () => {
  const {
    newAssistantOutputState,
    mergeSavedAssistantOutputs,
    reduceAssistantOutputEvent,
  } = await helpers()
  let state = mergeSavedAssistantOutputs(newAssistantOutputState(), [
    output({ next_batch_index: 2 }),
  ])
  state = reduceAssistantOutputEvent(
    state,
    event(100, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      step_index: 1,
      batch_index: 2,
      text: "!",
    }),
    "turn-a"
  )
  expect(state.outputs[0].content).toBe("Hello world!")
})

test("snapshot batch cursors ignore already persisted replayed batches", async () => {
  const {
    newAssistantOutputState,
    mergeSavedAssistantOutputs,
    reduceAssistantOutputEvent,
  } = await helpers()
  let state = mergeSavedAssistantOutputs(newAssistantOutputState(), [
    output({ next_batch_index: 2 }),
  ])
  for (const [sequence, batchIndex, text] of [
    [1, 0, "Hello"],
    [2, 1, " world"],
  ]) {
    state = reduceAssistantOutputEvent(
      state,
      event(sequence, "assistant.delta", {
        output_id: "output-a",
        model_call_id: "call-a",
        step_index: 1,
        batch_index: batchIndex,
        text,
      }),
      "turn-a"
    )
  }
  expect(state.outputs[0].content).toBe("Hello world")
  state = reduceAssistantOutputEvent(
    state,
    event(3, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      step_index: 1,
      batch_index: 2,
      text: "!",
    }),
    "turn-a"
  )
  expect(state.outputs[0].content).toBe("Hello world!")
})

test("a stale snapshot cannot rewind newer live text or its batch cursor", async () => {
  const {
    newAssistantOutputState,
    mergeSavedAssistantOutputs,
    reduceAssistantOutputEvent,
  } = await helpers()
  let state = newAssistantOutputState()
  for (const [sequence, batchIndex, text] of [
    [1, 0, "Hello"],
    [2, 1, " world"],
    [3, 2, "!"],
  ]) {
    state = reduceAssistantOutputEvent(
      state,
      event(sequence, "assistant.delta", {
        output_id: "output-a",
        model_call_id: "call-a",
        step_index: 1,
        batch_index: batchIndex,
        text,
      }),
      "turn-a"
    )
  }
  state = mergeSavedAssistantOutputs(state, [output({ next_batch_index: 2 })])
  state = reduceAssistantOutputEvent(
    state,
    event(4, "assistant.delta", {
      output_id: "output-a",
      model_call_id: "call-a",
      step_index: 1,
      batch_index: 3,
      text: " Again.",
    }),
    "turn-a"
  )
  expect(state.outputs[0].content).toBe("Hello world! Again.")
})
