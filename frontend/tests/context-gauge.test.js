import { expect, test } from "bun:test"

async function helpers() {
  return import("../apps/web/src/lib/context-gauge.ts")
}

function preview(changes = {}) {
  return {
    model_id: "model-a",
    context_window_tokens: 100000,
    input_tokens: 25000,
    count_method: "utf8_bytes_estimate_v1",
    output_reserve_tokens: 4096,
    safety_margin_tokens: 2048,
    usable_input_tokens: 93856,
    compaction_threshold_tokens: 75084,
    percentage: 25,
    status: "ready",
    checkpoint_id: null,
    ...changes,
  }
}

test("context details distinguish estimates from provider counts without changing known limits", async () => {
  const { contextGaugeDetails } = await helpers()
  const estimate = contextGaugeDetails(preview())
  expect(estimate.usage).toBe("~25,000 / 100,000 tokens")
  expect(estimate.percentage).toBe("~25% of model context")
  expect(estimate.outputReserve).toBe("4,096 tokens")
  expect(estimate.threshold).toBe("75,084 tokens")
  const counted = contextGaugeDetails(preview({ count_method: "provider" }))
  expect(counted.usage).toBe("25,000 / 100,000 tokens")
  expect(counted.percentage).toBe("25% of model context")
})

test("unavailable context limits stay unknown and malformed event values are ignored", async () => {
  const { parseContextPreview, contextGaugeDetails } = await helpers()
  expect(parseContextPreview(preview({ percentage: Number.NaN }))).toBeNull()
  expect(parseContextPreview(preview({ input_tokens: -1 }))).toBeNull()
  expect(parseContextPreview(preview({ status: "future_status" }))).toBeNull()
  expect(
    parseContextPreview(preview({ count_method: { toString: "invalid" } }))
  ).toBeNull()
  const unavailable = parseContextPreview(
    preview({
      status: "unavailable",
      context_window_tokens: null,
      input_tokens: null,
      output_reserve_tokens: null,
      safety_margin_tokens: null,
      usable_input_tokens: null,
      compaction_threshold_tokens: null,
      percentage: null,
    })
  )
  expect(unavailable.status).toBe("unavailable")
  expect(contextGaugeDetails(unavailable).usage).toBe("Unavailable")
})

test("ordered run context ignores duplicate events and stays tied to the locked model", async () => {
  const { newRunContextState, reduceRunContext, activeRunContext } =
    await helpers()
  let state = newRunContextState()
  const event = (sequence, eventType, data) => ({
    runId: "run-a",
    sequence,
    eventType,
    data,
  })
  state = reduceRunContext(state, event(3, "run.context", preview()))
  state = reduceRunContext(
    state,
    event(3, "run.context", preview({ input_tokens: 90000 }))
  )
  expect(activeRunContext(state, "run-a", "model-a").input_tokens).toBe(25000)
  expect(activeRunContext(state, "run-a", "other-model")).toBeNull()
  state = reduceRunContext(
    state,
    event(4, "run.phase", { phase: "compacting" })
  )
  expect(activeRunContext(state, "run-a", "model-a").status).toBe("compacting")
  state = reduceRunContext(
    state,
    event(
      5,
      "run.context",
      preview({ input_tokens: 12000, checkpoint_id: "checkpoint" })
    )
  )
  expect(activeRunContext(state, "run-a", "model-a").status).toBe("ready")
  expect(activeRunContext(state, "run-a", "model-a").checkpoint_id).toBe(
    "checkpoint"
  )
  expect(activeRunContext(state, "another-run", "model-a")).toBeNull()
})

test("hover, keyboard focus and taps open details while Escape and blur close them", async () => {
  const { newGaugeDisclosure, reduceGaugeDisclosure, isGaugeOpen } =
    await helpers()
  let state = newGaugeDisclosure()
  state = reduceGaugeDisclosure(state, "hover")
  expect(isGaugeOpen(state)).toBe(true)
  state = reduceGaugeDisclosure(state, "leave")
  expect(isGaugeOpen(state)).toBe(false)
  state = reduceGaugeDisclosure(state, "focus")
  state = reduceGaugeDisclosure(state, "tap")
  expect(isGaugeOpen(state)).toBe(true)
  state = reduceGaugeDisclosure(state, "tap")
  expect(isGaugeOpen(state)).toBe(false)
  state = reduceGaugeDisclosure(state, "focus")
  expect(isGaugeOpen(state)).toBe(true)
  state = reduceGaugeDisclosure(state, "escape")
  expect(isGaugeOpen(state)).toBe(false)
  state = reduceGaugeDisclosure(state, "hover")
  state = reduceGaugeDisclosure(state, "blur")
  expect(isGaugeOpen(state)).toBe(false)
})

test("document Escape dismisses hover details while focus stays outside the gauge", async () => {
  const {
    newGaugeDisclosure,
    reduceGaugeDisclosure,
    isGaugeOpen,
    listenForContextEscape,
  } = await helpers()
  expect(listenForContextEscape).toBeTypeOf("function")
  const documentTarget = new EventTarget()
  const keyDown = (key) => {
    const event = new Event("keydown", { cancelable: true })
    Object.defineProperty(event, "key", { value: key })
    return event
  }
  let state = reduceGaugeDisclosure(newGaugeDisclosure(), "hover")
  const stopListening = listenForContextEscape(documentTarget, () => {
    state = reduceGaugeDisclosure(state, "escape")
  })

  expect(state.focused).toBe(false)
  documentTarget.dispatchEvent(keyDown("Enter"))
  expect(isGaugeOpen(state)).toBe(true)
  const escape = keyDown("Escape")
  documentTarget.dispatchEvent(escape)
  expect(isGaugeOpen(state)).toBe(false)
  expect(escape.defaultPrevented).toBe(false)

  stopListening()
  state = reduceGaugeDisclosure(state, "hover")
  documentTarget.dispatchEvent(keyDown("Escape"))
  expect(isGaugeOpen(state)).toBe(true)
})

test("preview draft limits count Unicode characters and never shorten the submitted text", async () => {
  const { previewDraftTooLong } = await helpers()
  expect(previewDraftTooLong("a".repeat(32000))).toBe(false)
  expect(previewDraftTooLong("a".repeat(32001))).toBe(true)
  expect(previewDraftTooLong("🌳".repeat(32000))).toBe(false)
  expect(previewDraftTooLong("🌳".repeat(32001))).toBe(true)
})

test("preview changes debounce for 300ms and a cancelled callback cannot activate an old draft", async () => {
  const { delayContextPreview } = await helpers()
  const timers = []
  const cancelled = []
  const ready = []
  const schedule = (callback, delay) => {
    timers.push({ callback, delay })
    return () => cancelled.push(callback)
  }
  const cancelOld = delayContextPreview(
    "old-draft",
    (identity) => ready.push(identity),
    schedule
  )
  cancelOld()
  const cancelCurrent = delayContextPreview(
    "new-draft",
    (identity) => ready.push(identity),
    schedule
  )
  expect(timers.map((timer) => timer.delay)).toEqual([300, 300])
  timers[0].callback()
  expect(ready).toHaveLength(0)
  timers[1].callback()
  expect(ready).toEqual(["new-draft"])
  expect(cancelled).toHaveLength(1)
  cancelCurrent()
})
