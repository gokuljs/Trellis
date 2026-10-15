import type {
  AssistantOutput,
  LatestRun,
  Message,
  SessionHistoryPage,
  TimelineRow,
} from "@/lib/app-types"
import {
  mergeSavedAssistantOutputs,
  type AssistantOutputState,
} from "@/lib/assistant-outputs"

export function flattenHistoryPages(
  pages: SessionHistoryPage[]
): SessionHistoryPage {
  const chronological = [...pages].reverse()
  const messages = new Map<string, Message>()
  const outputs = new Map<string, AssistantOutput>()
  const rows = new Map<string, TimelineRow>()
  for (const page of chronological) {
    for (const message of page.messages) messages.set(message.id, message)
    for (const output of page.assistant_outputs ?? [])
      outputs.set(output.id, output)
    for (const row of page.timeline_order)
      rows.set(`${row.kind}:${row.id}`, row)
  }
  return {
    session: pages[0].session,
    messages: [...messages.values()],
    assistant_outputs: [...outputs.values()],
    timeline_order: [...rows.values()],
    next_before_cursor: pages.at(-1)?.next_before_cursor ?? null,
  }
}

export function hydrateOutputWindow(
  state: AssistantOutputState,
  saved: AssistantOutput[],
  activeRunId?: string | null
): AssistantOutputState {
  const visibleIds = new Set(saved.map((output) => output.id))
  const outputs = state.outputs.filter(
    (output) => visibleIds.has(output.id) || output.run_id === activeRunId
  )
  const keptIds = new Set(outputs.map((output) => output.id))
  const keptRuns = new Set(outputs.map((output) => output.run_id))
  return mergeSavedAssistantOutputs(
    {
      outputs,
      liveText: new Map([...state.liveText].filter(([id]) => keptIds.has(id))),
      lastSequence: new Map(
        [...state.lastSequence].filter(
          ([runId]) => keptRuns.has(runId) || runId === activeRunId
        )
      ),
    },
    saved
  )
}

export function retryInputForRun(
  sessionId: string,
  messages: Message[],
  latest: LatestRun
) {
  const input =
    latest.input_message ??
    messages.find(
      (message) => message.turn_id === latest.turn_id && message.role === "user"
    )
  if (!input || input.role !== "user" || input.turn_id !== latest.turn_id)
    return null
  return { sessionId, turnId: input.turn_id, content: input.content }
}
