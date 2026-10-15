import type { ContextPreview } from "@/lib/app-types"
import type { RuntimeRunEvent } from "@/lib/runtime-client"

const contextStatuses = new Set([
  "ready",
  "compaction_required",
  "compacting",
  "unavailable",
])
const numericFields = [
  "context_window_tokens",
  "input_tokens",
  "output_reserve_tokens",
  "safety_margin_tokens",
  "usable_input_tokens",
  "compaction_threshold_tokens",
  "percentage",
] as const

export function parseContextPreview(value: unknown): ContextPreview | null {
  if (typeof value !== "object" || value === null) return null
  const data = value as Record<string, unknown>
  if (
    typeof data.model_id !== "string" ||
    typeof data.count_method !== "string" ||
    !["provider", "utf8_bytes_estimate_v1"].includes(data.count_method) ||
    typeof data.status !== "string" ||
    !contextStatuses.has(data.status) ||
    !numericFields.every(
      (field) =>
        data[field] === null ||
        (typeof data[field] === "number" &&
          Number.isFinite(data[field]) &&
          data[field] >= 0)
    ) ||
    (data.checkpoint_id !== undefined &&
      data.checkpoint_id !== null &&
      typeof data.checkpoint_id !== "string")
  )
    return null
  return data as ContextPreview
}

export function contextGaugeDetails(preview: ContextPreview) {
  const estimate = preview.count_method !== "provider" ? "~" : ""
  const count = (value: number | null) =>
    value === null ? "Unavailable" : value.toLocaleString("en-US")
  return {
    usage:
      preview.input_tokens === null || preview.context_window_tokens === null
        ? "Unavailable"
        : `${estimate}${count(preview.input_tokens)} / ${count(preview.context_window_tokens)} tokens`,
    percentage:
      preview.percentage === null
        ? "Unavailable"
        : `${estimate}${Number(preview.percentage.toFixed(1))}% of model context`,
    outputReserve:
      preview.output_reserve_tokens === null
        ? "Unavailable"
        : `${count(preview.output_reserve_tokens)} tokens`,
    threshold:
      preview.compaction_threshold_tokens === null
        ? "Unavailable"
        : `${count(preview.compaction_threshold_tokens)} tokens`,
  }
}

export type RunContextState = {
  runId: string | null
  lastSequence: number
  preview: ContextPreview | null
  compacting: boolean
}

export function newRunContextState(): RunContextState {
  return { runId: null, lastSequence: 0, preview: null, compacting: false }
}

export function reduceRunContext(
  state: RunContextState,
  event: RuntimeRunEvent
): RunContextState {
  if (state.runId === event.runId && event.sequence <= state.lastSequence)
    return state
  const current = state.runId === event.runId ? state : newRunContextState()
  const next = { ...current, runId: event.runId, lastSequence: event.sequence }
  if (event.eventType === "run.context") {
    const preview = parseContextPreview(event.data)
    return preview
      ? { ...next, preview, compacting: preview.status === "compacting" }
      : next
  }
  if (event.eventType === "run.phase" && event.data.phase === "compacting")
    return { ...next, compacting: true }
  return next
}

export function activeRunContext(
  state: RunContextState,
  runId: string | null,
  modelId: string
): ContextPreview | null {
  if (state.runId !== runId || state.preview?.model_id !== modelId) return null
  return state.compacting
    ? { ...state.preview, status: "compacting" }
    : state.preview
}

export function previewDraftTooLong(draft: string): boolean {
  let length = 0
  let index = 0
  while (index < draft.length) {
    index += draft.codePointAt(index)! > 0xffff ? 2 : 1
    if (++length > 32_000) return true
  }
  return false
}

export function delayContextPreview(
  identity: string,
  ready: (identity: string) => void,
  schedule = (callback: () => void, delay: number) => {
    const timer = setTimeout(callback, delay)
    return () => clearTimeout(timer)
  }
): () => void {
  let current = true
  const cancel = schedule(() => {
    if (current) ready(identity)
  }, 300)
  return () => {
    current = false
    cancel()
  }
}

export type GaugeDisclosure = {
  hovered: boolean
  focused: boolean
  pinned: boolean
  dismissed: boolean
}
export type GaugeDisclosureEvent =
  "hover" | "leave" | "focus" | "blur" | "tap" | "escape"

export function newGaugeDisclosure(): GaugeDisclosure {
  return { hovered: false, focused: false, pinned: false, dismissed: false }
}

export function reduceGaugeDisclosure(
  state: GaugeDisclosure,
  event: GaugeDisclosureEvent
): GaugeDisclosure {
  switch (event) {
    case "hover":
      return { ...state, hovered: true, dismissed: false }
    case "leave":
      return { ...state, hovered: false }
    case "focus":
      return { ...state, focused: true, dismissed: false }
    case "blur":
      return newGaugeDisclosure()
    case "tap":
      return { ...state, pinned: !state.pinned, dismissed: state.pinned }
    case "escape":
      return { ...state, pinned: false, dismissed: true }
  }
}

export function isGaugeOpen(state: GaugeDisclosure): boolean {
  return !state.dismissed && (state.hovered || state.focused || state.pinned)
}

export function listenForContextEscape(
  target: EventTarget,
  dismiss: () => void
): () => void {
  const handleKeyDown = (event: Event) => {
    if ("key" in event && event.key === "Escape") dismiss()
  }
  target.addEventListener("keydown", handleKeyDown)
  return () => target.removeEventListener("keydown", handleKeyDown)
}
