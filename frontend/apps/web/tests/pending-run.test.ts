import assert from "node:assert/strict"
import { afterEach, test } from "bun:test"
import {
  clearPendingRun,
  clearUserPendingRuns,
  readPendingRun,
  savePendingRun,
} from "../src/lib/pending-run"

const storage = globalThis.localStorage

afterEach(() => {
  globalThis.localStorage = storage
})

test("pending metadata survives reloads without retaining submitted text", () => {
  const values: Record<string, string> = {}
  globalThis.localStorage = {
    getItem: (key: string) => values[key] ?? null,
    setItem: (key: string, value: string) => {
      values[key] = value
    },
    removeItem: (key: string) => {
      delete values[key]
    },
  } as unknown as Storage
  const pending = {
    userId: "alice",
    sessionId: "chat",
    turnId: "turn",
    clientRequestId: "request",
    runId: "run",
    lastSequence: 3,
    content: "Private submitted text",
  }
  savePendingRun(pending)
  assert.deepEqual(readPendingRun("alice", "chat"), {
    userId: "alice",
    sessionId: "chat",
    turnId: "turn",
    clientRequestId: "request",
    runId: "run",
    lastSequence: 3,
  })
  assert.equal(JSON.stringify(values).includes("Private submitted text"), false)
  assert.equal(readPendingRun("bob", "chat"), null)
  clearPendingRun("alice", "chat", "older-request")
  assert.ok(readPendingRun("alice", "chat"))
  clearPendingRun("alice", "chat", "request")
  assert.equal(readPendingRun("alice", "chat"), null)
})

test("sign-out cleanup is scoped to one user and tolerates unavailable storage", () => {
  const values: Record<string, string> = {}
  globalThis.localStorage = new Proxy(values, {
    get(target, property) {
      if (property === "getItem") return (key: string) => target[key] ?? null
      if (property === "setItem")
        return (key: string, value: string) => {
          target[key] = value
        }
      if (property === "removeItem")
        return (key: string) => {
          delete target[key]
        }
      return Reflect.get(target, property)
    },
  }) as unknown as Storage
  const pending = {
    userId: "alice",
    sessionId: "chat",
    turnId: "turn",
    clientRequestId: "request",
    runId: null,
    lastSequence: 0,
  }
  savePendingRun(pending)
  savePendingRun({ ...pending, userId: "bob" })
  clearUserPendingRuns("alice")
  assert.equal(readPendingRun("alice", "chat"), null)
  assert.equal(readPendingRun("bob", "chat")?.clientRequestId, "request")
  globalThis.localStorage = undefined as unknown as Storage
  assert.doesNotThrow(() => savePendingRun(pending))
  assert.equal(readPendingRun("alice", "chat"), null)
  assert.doesNotThrow(() => clearUserPendingRuns("alice"))
})

test("credential refresh and failed sign-out retain pending IDs until sign-out succeeds", async () => {
  const { createAuthController } = await import("../src/lib/auth-controller")
  const values: Record<string, string> = {}
  globalThis.localStorage = new Proxy(values, {
    get(target, property) {
      if (property === "getItem") return (key: string) => target[key] ?? null
      if (property === "setItem")
        return (key: string, value: string) => {
          target[key] = value
        }
      if (property === "removeItem")
        return (key: string) => {
          delete target[key]
        }
      return Reflect.get(target, property)
    },
  }) as unknown as Storage
  const originalWindow = globalThis.window
  globalThis.window = {
    location: { href: "https://trellis.test/", origin: "https://trellis.test" },
    history: { replaceState() {} },
    addEventListener() {},
    removeEventListener() {},
  } as unknown as Window & typeof globalThis
  let authEvent: (event: string, session: unknown) => void = () => {}
  let signOutError: unknown = new Error("Temporary sign-out failure")
  const user = { id: "alice" }
  const session = { user, access_token: "token" }
  const controller = createAuthController({
    auth: {
      getSession: async () => ({ data: { session }, error: null }),
      getUser: async () => ({ data: { user }, error: null }),
      signOut: async () => ({ error: signOutError }),
      onAuthStateChange: (listener: typeof authEvent) => {
        authEvent = listener
        return { data: { subscription: { unsubscribe() {} } } }
      },
    },
  } as unknown as Parameters<typeof createAuthController>[0])
  const unsubscribe = controller.subscribe(() => {})
  try {
    for (
      let attempt = 0;
      attempt < 20 && controller.getSnapshot().status !== "signed-in";
      attempt++
    )
      await new Promise((resolve) => setTimeout(resolve, 0))
    assert.equal(controller.getSnapshot().status, "signed-in")
    const pending = {
      userId: "alice",
      sessionId: "chat",
      turnId: "turn",
      clientRequestId: "request",
      runId: "run",
      lastSequence: 3,
    }
    savePendingRun(pending)
    authEvent("TOKEN_REFRESHED", { ...session, access_token: "new-token" })
    await new Promise((resolve) => setTimeout(resolve, 0))
    assert.equal(readPendingRun("alice", "chat")?.clientRequestId, "request")
    await controller.signOut()
    assert.equal(controller.getSnapshot().status, "signed-in")
    assert.equal(readPendingRun("alice", "chat")?.clientRequestId, "request")
    signOutError = null
    await controller.signOut()
    assert.equal(controller.getSnapshot().status, "signed-out")
    assert.equal(readPendingRun("alice", "chat"), null)
  } finally {
    unsubscribe()
    globalThis.window = originalWindow
  }
})
