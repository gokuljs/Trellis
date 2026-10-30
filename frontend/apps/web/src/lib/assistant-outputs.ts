import type { AssistantOutput, Message, TimelineRow } from "@/lib/app-types"
import type { RuntimeRunEvent } from "@/lib/runtime-client"

type LiveText = { content: string; lastBatchIndex: number }

export type AssistantOutputState = {
  outputs: AssistantOutput[]
  liveText: ReadonlyMap<string, LiveText>
  lastSequence: ReadonlyMap<string, number>
}

export function newAssistantOutputState(): AssistantOutputState {
  return { outputs: [], liveText: new Map(), lastSequence: new Map() }
}

function longestPublishedPrefix(previous: string, incoming: string) {
  return previous.startsWith(incoming) ? previous : incoming
}

export function mergeSavedAssistantOutputs(
  state: AssistantOutputState,
  saved: AssistantOutput[]
): AssistantOutputState {
  const outputs = new Map(state.outputs.map((output) => [output.id, output]))
  const liveText = new Map(state.liveText)
  for (const output of saved) {
    const previous = outputs.get(output.id)
    if (
      previous &&
      previous.status !== "streaming" &&
      output.status === "streaming"
    )
      continue
    const nextBatchIndex = output.next_batch_index
    if (
      typeof nextBatchIndex === "number" &&
      Number.isSafeInteger(nextBatchIndex) &&
      nextBatchIndex >= 0
    ) {
      const live = liveText.get(output.id)
      const savedCursor = nextBatchIndex - 1
      if (!live || live.lastBatchIndex <= savedCursor) {
        liveText.set(output.id, {
          content: output.content,
          lastBatchIndex: savedCursor,
        })
      }
    }
    outputs.set(
      output.id,
      previous && output.status === "streaming"
        ? {
            ...output,
            content: longestPublishedPrefix(previous.content, output.content),
            next_batch_index:
              previous.next_batch_index !== undefined ||
              nextBatchIndex !== undefined
                ? Math.max(previous.next_batch_index ?? 0, nextBatchIndex ?? 0)
                : undefined,
          }
        : output
    )
  }
  return { ...state, outputs: [...outputs.values()], liveText }
}

export function reduceAssistantOutputEvent(
  state: AssistantOutputState,
  event: RuntimeRunEvent,
  turnId: string
): AssistantOutputState {
  if (event.sequence <= (state.lastSequence.get(event.runId) ?? 0)) return state
  const lastSequence = new Map(state.lastSequence)
  lastSequence.set(event.runId, event.sequence)
  if (
    [
      "run.failed",
      "run.cancelled",
      "run.interrupted",
      "run.completed",
    ].includes(event.eventType)
  ) {
    const reason =
      typeof event.data.code === "string"
        ? event.data.code
        : event.eventType.slice(4)
    return {
      ...state,
      lastSequence,
      outputs: state.outputs.map((output) =>
        output.run_id === event.runId && output.status === "streaming"
          ? { ...output, status: "partial", stop_reason: reason }
          : output
      ),
    }
  }
  const outputId = event.data.output_id
  if (typeof outputId !== "string") return state
  const previous = state.outputs.find((output) => output.id === outputId)
  if (
    previous &&
    (previous.run_id !== event.runId || previous.turn_id !== turnId)
  )
    return state
  if (["assistant.message", "assistant.completed"].includes(event.eventType)) {
    if (
      !previous ||
      previous.status === "partial" ||
      event.data.status !== "complete"
    )
      return state
    const promoted =
      event.data.promoted_message_id ??
      (event.eventType === "assistant.completed" ? event.data.message_id : null)
    const completed: AssistantOutput = {
      ...previous,
      content:
        typeof event.data.content === "string"
          ? event.data.content
          : previous.content,
      status: "complete",
      stop_reason:
        typeof event.data.stop_reason === "string"
          ? event.data.stop_reason
          : previous.stop_reason,
      promoted_message_id:
        typeof promoted === "string" ? promoted : previous.promoted_message_id,
    }
    return {
      ...state,
      lastSequence,
      outputs: state.outputs.map((item) =>
        item.id === outputId ? completed : item
      ),
    }
  }
  if (event.eventType !== "assistant.delta") return state
  const text = event.data.text
  const callId = event.data.model_call_id
  const batchIndex = event.data.batch_index
  if (
    typeof text !== "string" ||
    typeof callId !== "string" ||
    typeof batchIndex !== "number" ||
    !Number.isSafeInteger(batchIndex) ||
    batchIndex < 0
  )
    return state
  if (previous && previous.status !== "streaming") return state
  const live = state.liveText.get(outputId)
  if (live && batchIndex <= live.lastBatchIndex) return state
  const content = (live?.content ?? "") + text
  const liveText = new Map(state.liveText)
  liveText.set(outputId, { content, lastBatchIndex: batchIndex })
  const output: AssistantOutput = {
    id: outputId,
    turn_id: turnId,
    run_id: event.runId,
    model_call_id: callId,
    step_index:
      typeof event.data.step_index === "number"
        ? event.data.step_index
        : (previous?.step_index ?? 1),
    content: longestPublishedPrefix(previous?.content ?? "", content),
    status: "streaming",
    stop_reason: null,
    promoted_message_id: null,
    created_at: previous?.created_at ?? event.createdAt ?? "",
    next_batch_index: batchIndex + 1,
  }
  return {
    outputs: previous
      ? state.outputs.map((item) => (item.id === outputId ? output : item))
      : [...state.outputs, output],
    liveText,
    lastSequence,
  }
}

export function outputStatusLabel(output: AssistantOutput): string {
  if (output.status === "streaming") return "Generating"
  if (output.status === "complete")
    return output.promoted_message_id ? "" : "Working note"
  if (
    ["cancelled", "user_cancelled", "run_cancelled"].includes(
      output.stop_reason ?? ""
    )
  )
    return "Partial answer · Stopped"
  if (
    ["interrupted", "runtime_shutdown", "run_interrupted"].includes(
      output.stop_reason ?? ""
    )
  )
    return "Partial answer · Interrupted"
  return "Partial answer"
}

export type ThreadMessage = Message & {
  output: AssistantOutput | null
  reportOutput: AssistantOutput | null
  attemptNumber: number | null
}

export function threadMessages(
  messages: Message[],
  outputs: AssistantOutput[],
  runOrderByTurn: Record<string, string[]> = {},
  timelineOrder?: TimelineRow[]
): ThreadMessage[] {
  const orderedOutputs = [
    ...new Map(outputs.map((output) => [output.id, output])).values(),
  ].sort((left, right) => {
    const leftTime = Date.parse(left.created_at)
    const rightTime = Date.parse(right.created_at)
    return (
      (Number.isFinite(leftTime) ? leftTime : 0) -
        (Number.isFinite(rightTime) ? rightTime : 0) ||
      left.step_index - right.step_index
    )
  })
  const runIdsByTurn = new Map<string, string[]>()
  for (const output of orderedOutputs) {
    const runIds = runIdsByTurn.get(output.turn_id) ?? [
      ...new Set(runOrderByTurn[output.turn_id] ?? []),
    ]
    if (!runIds.includes(output.run_id)) runIds.push(output.run_id)
    runIdsByTurn.set(output.turn_id, runIds)
  }
  const attemptNumber = (output: AssistantOutput | undefined) => {
    if (!output) return null
    const runs = runIdsByTurn.get(output.turn_id) ?? []
    return runs.length > 1 ? runs.indexOf(output.run_id) + 1 : null
  }
  const outputMessage = (output: AssistantOutput): ThreadMessage => ({
    id: `output-${output.id}`,
    turn_id: output.turn_id,
    role: "assistant",
    content: output.content,
    provider: null,
    model: null,
    created_at: output.created_at,
    output,
    reportOutput: output,
    attemptNumber: attemptNumber(output),
  })
  const byTurn = new Map<string, AssistantOutput[]>()
  for (const output of orderedOutputs) {
    const promoted = messages.some(
      (message) =>
        message.role === "assistant" &&
        message.id === output.promoted_message_id &&
        message.turn_id === output.turn_id
    )
    if (!output.content || promoted) continue
    const turn = byTurn.get(output.turn_id) ?? []
    turn.push(output)
    byTurn.set(output.turn_id, turn)
  }
  for (const [turnId, turn] of byTurn) {
    const runs = runIdsByTurn.get(turnId) ?? []
    turn.sort(
      (left, right) =>
        runs.indexOf(left.run_id) - runs.indexOf(right.run_id) ||
        left.step_index - right.step_index
    )
  }
  const entries: ThreadMessage[] = []
  const attachedTurns = new Set<string>()
  for (const message of messages) {
    const promoted = orderedOutputs.find(
      (output) =>
        output.promoted_message_id === message.id &&
        output.turn_id === message.turn_id
    )
    entries.push({
      ...message,
      output: null,
      reportOutput: promoted ?? null,
      attemptNumber: attemptNumber(promoted),
    })
    if (message.role !== "user" || attachedTurns.has(message.turn_id)) continue
    attachedTurns.add(message.turn_id)
    for (const output of byTurn.get(message.turn_id) ?? []) {
      entries.push(outputMessage(output))
    }
  }
  for (const [turnId, turn] of byTurn) {
    if (!attachedTurns.has(turnId)) entries.push(...turn.map(outputMessage))
  }
  if (timelineOrder) {
    const ranks = new Map(
      timelineOrder.map((row, index) => [`${row.kind}:${row.id}`, index])
    )
    entries.sort((left, right) => {
      const key = (entry: ThreadMessage) =>
        entry.output
          ? `assistant_output:${entry.output.id}`
          : `message:${entry.id}`
      return (
        (ranks.get(key(left)) ?? Infinity) - (ranks.get(key(right)) ?? Infinity)
      )
    })
  }
  return entries
}
