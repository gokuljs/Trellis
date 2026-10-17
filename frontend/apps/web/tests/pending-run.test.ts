import { describe, expect, test } from "bun:test"

import {
  clearPendingRuns,
  forgetPendingRun,
  readPendingRun,
  rememberPendingRun,
} from "../src/lib/pending-run"

function storage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() {
      return values.size
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  }
}

const input = {
  sessionId: "chat",
  turnId: "turn",
  clientRequestId: "request",
  content: "Private message text",
  modelId: "model",
}

describe("pending request identity", () => {
  test("reload retains identity without storing message text", () => {
    const target = storage()
    rememberPendingRun("owner", input, target)
    expect(readPendingRun("owner", "chat", target)).toEqual({
      sessionId: "chat",
      turnId: "turn",
      clientRequestId: "request",
      modelId: "model",
    })
    expect(target.getItem(target.key(0)!)).not.toContain(input.content)
    expect(
      rememberPendingRun(
        "owner",
        { ...input, clientRequestId: "new-id" },
        target
      ).clientRequestId
    ).toBe("request")
  })

  test("another owner cannot recover or clear the request", () => {
    const target = storage()
    rememberPendingRun("owner", input, target)
    expect(readPendingRun("other", "chat", target)).toBeNull()
    forgetPendingRun("other", "chat", undefined, target)
    expect(readPendingRun("owner", "chat", target)?.clientRequestId).toBe(
      "request"
    )
  })

  test("an explicit new turn or model creates a separate request", () => {
    const target = storage()
    rememberPendingRun("owner", input, target)
    expect(
      rememberPendingRun(
        "owner",
        { ...input, modelId: "changed", clientRequestId: "changed-model" },
        target
      ).clientRequestId
    ).toBe("changed-model")
    expect(
      rememberPendingRun(
        "owner",
        { ...input, turnId: "next-turn", clientRequestId: "next-request" },
        target
      ).clientRequestId
    ).toBe("next-request")
  })

  test("old completion cannot clear a newer request", () => {
    const target = storage()
    rememberPendingRun("owner", input, target)
    forgetPendingRun("owner", "chat", "older-request", target)
    expect(readPendingRun("owner", "chat", target)).not.toBeNull()
    forgetPendingRun("owner", "chat", "request", target)
    expect(readPendingRun("owner", "chat", target)).toBeNull()
  })

  test("sign-out clears request metadata and keeps unrelated storage", () => {
    const target = storage()
    target.setItem("theme", "dark")
    rememberPendingRun("owner", input, target)
    rememberPendingRun("other", input, target)
    clearPendingRuns(target)
    expect(target.length).toBe(1)
    expect(target.getItem("theme")).toBe("dark")
  })

  test("corrupt or unavailable browser storage does not stop submission", () => {
    const target = storage()
    rememberPendingRun("owner", input, target)
    target.setItem(target.key(0)!, "not json")
    expect(readPendingRun("owner", "chat", target)).toBeNull()
    expect(rememberPendingRun("owner", input, null)).toEqual(input)
    expect(() => clearPendingRuns(null)).not.toThrow()
  })
})
