import { expect, test } from "bun:test"

async function helpers() {
  return import("../apps/web/src/lib/model-choice.ts")
}

function model(id, changes = {}) {
  return {
    id,
    name: id,
    configured: true,
    requires_api_key: true,
    ...changes,
  }
}

test("saved chat choices stay independent of the default and new-chat draft", async () => {
  const { selectedChatModelId } = await helpers()
  const settings = { selected_model_id: "default", models: [model("default")] }
  expect(selectedChatModelId({ model_id: "saved" }, settings, "draft")).toBe(
    "saved"
  )
  expect(selectedChatModelId(null, settings, "draft")).toBe("draft")
  expect(selectedChatModelId(null, settings, null)).toBe("default")
  expect(selectedChatModelId({ model_id: "unknown" }, settings, null)).toBe(
    "unknown"
  )
})

test("legacy chats use the default and an absent default uses the first selectable model", async () => {
  const { selectedChatModelId } = await helpers()
  expect(
    selectedChatModelId({}, { selected_model_id: "default" }, "draft")
  ).toBe("default")
  expect(
    selectedChatModelId(null, {
      models: [model("pending", { selectable: false }), model("ready")],
    })
  ).toBe("ready")
})

test("a model save acknowledged after returning to a chat overrides its stale local preference", async () => {
  const { selectedChatModelId } = await helpers()
  const session = { id: "chat-a", model_id: "old" }
  const settings = { selected_model_id: "default" }
  expect(
    selectedChatModelId(session, settings, null, {
      id: "chat-a",
      model_id: "saved",
    })
  ).toBe("saved")
  expect(
    selectedChatModelId(session, settings, null, {
      id: "chat-b",
      model_id: "other",
    })
  ).toBe("old")
  expect(
    selectedChatModelId(null, settings, "draft", {
      id: "chat-a",
      model_id: "saved",
    })
  ).toBe("draft")
})

test("pending metadata, unavailable providers and disabled models cannot be selected", async () => {
  const { isModelSelectable, modelAvailabilityLabel } = await helpers()
  const pending = model("pending", { metadata_status: "pending" })
  expect(isModelSelectable(pending)).toBe(false)
  expect(modelAvailabilityLabel(pending)).toBe("Pending")
  expect(isModelSelectable(model("blocked", { selectable: false }))).toBe(false)
  expect(isModelSelectable(model("disabled", { enabled: false }))).toBe(false)
  expect(
    isModelSelectable(
      model("known", { selectable: true, availability: "unknown" })
    )
  ).toBe(true)
  expect(
    isModelSelectable(model("unavailable", { availability: "unavailable" }))
  ).toBe(false)
  expect(
    isModelSelectable(model("no-key", { selectable: true, configured: false }))
  ).toBe(false)
  expect(isModelSelectable(model("no-key", { configured: false }))).toBe(false)
  expect(isModelSelectable(undefined)).toBe(false)
  expect(isModelSelectable(model("ready", { selectable: true }))).toBe(true)
  expect(
    isModelSelectable(
      model("managed", { configured: false, requires_api_key: false })
    )
  ).toBe(true)
})

test("keyboard model selection skips unavailable choices and respects Home and End", async () => {
  const { nextSelectableModelIndex } = await helpers()
  const models = [
    model("pending", { selectable: false }),
    model("first"),
    model("no-key", { configured: false }),
    model("last"),
  ]
  expect(nextSelectableModelIndex(models, 1, "ArrowDown")).toBe(3)
  expect(nextSelectableModelIndex(models, 3, "ArrowRight")).toBe(1)
  expect(nextSelectableModelIndex(models, 1, "ArrowUp")).toBe(3)
  expect(nextSelectableModelIndex(models, 3, "Home")).toBe(1)
  expect(nextSelectableModelIndex(models, 1, "End")).toBe(3)
  expect(nextSelectableModelIndex([models[0]], 0, "ArrowDown")).toBeNull()
  expect(nextSelectableModelIndex(models, 1, "Tab")).toBeNull()
})

test("model-save ownership rejects older requests and chat generations", async () => {
  const { ownsModelSelection } = await helpers()
  const owner = { sessionId: "chat-a", generation: 4, requestSequence: 2 }
  expect(ownsModelSelection(owner, owner)).toBe(true)
  expect(ownsModelSelection(owner, { ...owner, requestSequence: 3 })).toBe(
    false
  )
  expect(ownsModelSelection(owner, { ...owner, sessionId: "chat-b" })).toBe(
    false
  )
  expect(ownsModelSelection(owner, { ...owner, generation: 6 })).toBe(false)
})

test("a new chat waits for its draft model to be saved before it can run", async () => {
  const { ensureSessionModel } = await helpers()
  const session = { id: "chat-a", model_id: "default" }
  let release
  const saved = new Promise((resolve) => (release = resolve))
  const calls = []
  let prepared = false
  const preparing = ensureSessionModel(session, "draft", (chatId, modelId) => {
    calls.push({ chatId, modelId })
    return saved
  }).then((result) => {
    prepared = true
    return result
  })
  await Promise.resolve()
  expect(prepared).toBe(false)
  expect(calls).toEqual([{ chatId: "chat-a", modelId: "draft" }])
  release({ ...session, model_id: "draft" })
  expect((await preparing).model_id).toBe("draft")
})

test("a seeded matching model avoids another save and a failed save prevents preparation", async () => {
  const { ensureSessionModel } = await helpers()
  const session = { id: "chat-a", model_id: "ready" }
  const calls = []
  expect(
    await ensureSessionModel(session, "ready", (...args) => calls.push(args))
  ).toBe(session)
  expect(calls).toHaveLength(0)
  await expect(
    ensureSessionModel(session, "draft", async () => {
      throw new Error("Not saved")
    })
  ).rejects.toThrow("Not saved")
})
