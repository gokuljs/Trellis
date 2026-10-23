import assert from "node:assert/strict"
import { afterEach, test, mock, spyOn } from "bun:test"

import * as authModule from "../src/lib/auth-controller"
import {
  AuthAccessError,
  type AuthController,
} from "../src/lib/auth-controller"
import {
  cancelRun,
  lookupRun,
  respondToToolApproval,
  resumeRun,
  streamRun,
} from "../src/lib/runtime-client"
import { logFailure } from "../src/lib/logging"
import {
  isReconciledRunOutcome,
  ownsRun,
  turnFailureAction,
} from "../src/lib/run-recovery"

const secret = "private-runtime-token-prompt-session-tool"
const originalSocket = globalThis.WebSocket
const originalWindow = globalThis.window
const nativeSetTimeout = globalThis.setTimeout
const nativeClearTimeout = globalThis.clearTimeout
const sockets: FakeSocket[] = []
const deadlines = new Map<ReturnType<typeof setTimeout>, () => void>()
const records: Record<string, unknown>[] = []

class FakeSocket {
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  sent: { id: string; method: string; params: Record<string, unknown> }[] = []
  closed = false
  constructor() {
    sockets.push(this)
  }
  send(value: string) {
    this.sent.push(JSON.parse(value))
  }
  close() {
    if (!this.closed) {
      this.closed = true
      this.onclose?.({ code: 1000 })
    }
  }
  receive(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) })
  }
  disconnect() {
    this.closed = true
    this.onclose?.({ code: 1006 })
  }
}

afterEach(() => {
  for (const socket of sockets.splice(0)) socket.close()
  mock.restore()
  globalThis.WebSocket = originalSocket
  globalThis.window = originalWindow
  deadlines.clear()
})

async function until(predicate: () => boolean) {
  for (let attempt = 0; attempt < 30; attempt++) {
    if (predicate()) return
    await new Promise<void>((resolve) => nativeSetTimeout(resolve, 0))
  }
  assert.fail("Runtime did not reach the expected state")
}

function setup() {
  let epoch = 1
  const listeners = new Set<() => void>()
  let credential = { token: secret, userId: secret, sessionEpoch: epoch }
  const refreshAccessToken = mock(async () => {
    credential = { ...credential, token: "refreshed-token" }
    return credential
  })
  const expireSession = mock(async () => undefined)
  const auth = {
    getSnapshot: () => ({ status: "signed-in", user: { id: secret } }),
    getSessionEpoch: () => epoch,
    getAccessToken: async () => credential,
    refreshAccessToken,
    isCredentialCurrent: () => epoch === credential.sessionEpoch,
    expireSession,
    subscribe: (listener: () => void) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
  } as unknown as AuthController
  spyOn(authModule, "getAuthController").mockReturnValue(auth)
  spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url
    const pathname = new URL(url, "https://trellis.test").pathname
    assert.ok(pathname === "/health" || pathname === "/ready")
    return Response.json({ status: pathname === "/ready" ? "ready" : "ok" })
  })
  globalThis.WebSocket = FakeSocket as unknown as typeof WebSocket
  globalThis.window = {
    location: { href: "https://trellis.test/" },
  } as Window & typeof globalThis
  records.length = 0
  spyOn(console, "error").mockImplementation((record: unknown) => {
    records.push(record as Record<string, unknown>)
  })
  spyOn(console, "warn").mockImplementation((record: unknown) => {
    records.push(record as Record<string, unknown>)
  })
  spyOn(globalThis, "setTimeout").mockImplementation(((
    handler: () => void,
    delay?: number
  ) => {
    if (delay !== 30_000) return nativeSetTimeout(handler, delay)
    const timer = nativeSetTimeout(() => undefined, 0)
    nativeClearTimeout(timer)
    deadlines.set(timer, handler)
    return timer
  }) as typeof setTimeout)
  spyOn(globalThis, "clearTimeout").mockImplementation(((
    timer: ReturnType<typeof setTimeout>
  ) => {
    deadlines.delete(timer)
    nativeClearTimeout(timer)
  }) as typeof clearTimeout)
  return {
    refreshAccessToken,
    expireSession,
    changeAccount() {
      epoch++
      for (const listener of listeners) listener()
    },
    recorded() {
      assert.equal(JSON.stringify(records).includes(secret), false)
      return records
    },
  }
}

async function authenticate(index = 0) {
  await until(() => !!sockets[index])
  const socket = sockets[index]!
  socket.onopen?.()
  await until(() =>
    socket.sent.some((frame) => frame.method === "auth.authenticate")
  )
  const auth = socket.sent.find(
    (frame) => frame.method === "auth.authenticate"
  )!
  socket.receive({ jsonrpc: "2.0", id: auth.id, result: { userId: secret } })
  await until(() =>
    socket.sent.some((frame) => frame.method !== "auth.authenticate")
  )
  return socket
}

function acknowledge(socket: FakeSocket, result: unknown) {
  const request = socket.sent.find(
    (frame) => frame.method !== "auth.authenticate"
  )!
  socket.receive({ jsonrpc: "2.0", id: request.id, result })
}

function complete(socket: FakeSocket) {
  socket.receive({
    jsonrpc: "2.0",
    method: "run.event",
    params: {
      runId: secret,
      sequence: 1,
      eventType: "run.completed",
      eventVersion: 1,
      data: { content: secret },
    },
  })
}

test("start and resume preserve run completion behavior without reporting successes", async () => {
  const { recorded } = setup()
  const started = streamRun({
    sessionId: secret,
    turnId: secret,
    clientRequestId: secret,
    content: secret,
  })
  const first = await authenticate()
  acknowledge(first, { runId: secret, modelId: secret })
  await until(() => first.sent.length === 2)
  complete(first)
  assert.deepEqual(await started, { runId: secret })
  const resumed = resumeRun(secret)
  const second = await authenticate(1)
  assert.equal(second.sent[1]?.method, "run.resume")
  acknowledge(second, { runId: secret })
  complete(second)
  assert.deepEqual(await resumed, { runId: secret })
  assert.deepEqual(recorded(), [])
})

test("cancel and respond acknowledgements validate returned results", async () => {
  const { recorded } = setup()
  const cancel = cancelRun(secret)
  const first = await authenticate()
  acknowledge(first, { runId: secret, status: "cancelling", private: secret })
  await cancel
  const respond = respondToToolApproval(secret, secret, "approved")
  const second = await authenticate(1)
  acknowledge(second, {
    runId: secret,
    toolCallId: secret,
    decision: "denied",
    private: secret,
  })
  await assert.rejects(
    respond,
    (error) =>
      error instanceof Error && error.message.includes("different approval")
  )
  assert.deepEqual(recorded(), [])
})

test("timeout and account teardown settle pending requests", async () => {
  const { recorded, changeAccount } = setup()
  const timed = cancelRun(secret)
  await authenticate()
  for (const handler of [...deadlines.values()]) handler()
  await assert.rejects(timed)
  const stale = resumeRun(secret)
  await authenticate(1)
  changeAccount()
  await assert.rejects(stale)
  assert.deepEqual(recorded(), [])
})

test("successful transport reconnect stays quiet and exhausted reconnect reports only at the final caller", async () => {
  const { recorded } = setup()
  const recovering = resumeRun(secret, {
    reconnectAttempts: 1,
    reconnectDelayMs: 0,
  })
  const first = await authenticate()
  acknowledge(first, { runId: secret })
  first.disconnect()
  const second = await authenticate(1)
  acknowledge(second, { runId: secret })
  complete(second)
  await recovering
  assert.deepEqual(recorded(), [])
  const exhausted = resumeRun(secret, { reconnectAttempts: 0 })
  const third = await authenticate(2)
  third.disconnect()
  await assert.rejects(exhausted, (error) => {
    logFailure(error, "run.restore")
    logFailure(error, "run.restore")
    return true
  })
  assert.equal(recorded().length, 1)
  assert.equal(recorded()[0]?.operation, "run.restore")
})

test("invalid start and cancellation responses still reject", async () => {
  const { recorded } = setup()
  const started = streamRun({
    sessionId: secret,
    turnId: secret,
    clientRequestId: secret,
    content: secret,
  })
  const first = await authenticate()
  acknowledge(first, { runId: 123, private: secret })
  await assert.rejects(started)
  const cancel = cancelRun(secret)
  const second = await authenticate(1)
  acknowledge(second, { runId: "different-run", status: "cancelling" })
  await assert.rejects(cancel)
  const resumed = resumeRun(secret)
  const third = await authenticate(2)
  third.onmessage?.({ data: "not-json" })
  await assert.rejects(resumed)
  assert.deepEqual(recorded(), [])
})

test("RPC rejection and successful approval preserve request outcomes", async () => {
  const { recorded } = setup()
  const failed = respondToToolApproval(secret, secret, "approved")
  const first = await authenticate()
  first.receive({
    jsonrpc: "2.0",
    id: "runtime-respond",
    error: {
      code: -1,
      message: secret,
      data: { code: "service_unavailable", status: 503 },
    },
  })
  await assert.rejects(failed)
  const approved = respondToToolApproval(secret, secret, "approved")
  const second = await authenticate(1)
  acknowledge(second, {
    runId: secret,
    toolCallId: secret,
    decision: "approved",
  })
  await approved
  assert.deepEqual(recorded(), [])
})

test("reconciliation accepts terminal outcomes and suppresses stale owners", async () => {
  const { recorded } = setup()
  for (const status of ["completed", "cancelled", "interrupted"]) {
    assert.equal(isReconciledRunOutcome(status), true)
  }
  for (const status of ["running", "queued", "failed", null, undefined]) {
    assert.equal(isReconciledRunOutcome(status), false)
  }
  const owner = { sessionId: secret, generation: 1 }
  const current = { sessionId: "new-session", generation: 2 }
  if (ownsRun(owner, current)) logFailure(new Error(secret), "run.restore")
  assert.deepEqual(recorded(), [])
})

test("unconfirmed acceptance failures require discovery before unlocking", () => {
  for (const code of [
    "auth_unavailable",
    "database_unavailable",
    "runtime_protocol_error",
    "run_in_progress",
    "session_run_busy",
    undefined,
  ]) {
    assert.equal(turnFailureAction(code, null), "reconnect", code)
  }
  assert.equal(turnFailureAction("run_capacity", null, true), "reconnect")
})

test("definite rejection and terminal events allow a new explicit attempt", () => {
  for (const code of [
    "run_capacity",
    "run_token_too_short",
    "message_empty",
    "message_too_large",
    "invalid_budget_preset",
    "provider_not_configured",
  ]) {
    assert.equal(turnFailureAction(code, null), "retry", code)
  }
  assert.equal(turnFailureAction("database_unavailable", "failed"), "retry")
  assert.equal(turnFailureAction("database_unavailable", "completed"), "reload")
})

test("leaving a running chat closes its browser subscription without cancelling the run", async () => {
  setup()
  const connection = new AbortController()
  const events: string[] = []
  const followed = streamRun(
    {
      sessionId: secret,
      turnId: secret,
      clientRequestId: secret,
      content: secret,
    },
    {
      signal: connection.signal,
      onEvent: (event) => events.push(event.eventType),
    }
  )
  const socket = await authenticate()
  acknowledge(socket, { runId: secret })
  await until(() => socket.sent.length === 2)
  connection.abort()
  await assert.rejects(followed)
  complete(socket)
  assert.equal(socket.closed, true)
  assert.deepEqual(
    socket.sent.map((frame) => frame.method),
    ["auth.authenticate", "run.start"]
  )
  assert.deepEqual(events, [])
})

test("request lookup accepts an absent request without creating a run", async () => {
  setup()
  const missing = lookupRun("chat", "request")
  const first = await authenticate()
  assert.equal(first.sent[1]?.method, "run.lookup")
  assert.deepEqual(first.sent[1]?.params, {
    sessionId: "chat",
    clientRequestId: "request",
  })
  acknowledge(first, null)
  assert.equal(await missing, null)
  const found = lookupRun("chat", "request")
  const second = await authenticate(1)
  acknowledge(second, { runId: secret, status: "running", modelId: "model" })
  assert.deepEqual(await found, {
    runId: secret,
    status: "running",
    modelId: "model",
  })
  assert.equal(
    sockets
      .flatMap((socket) => socket.sent)
      .some((frame) => frame.method === "run.start"),
    false
  )
})

test("a lost acceptance reply reconnects with the original request ID", async () => {
  setup()
  const input = {
    sessionId: "chat",
    turnId: "turn",
    clientRequestId: "request",
    content: "Hello",
  }
  const started = streamRun(input, {
    reconnectAttempts: 1,
    reconnectDelayMs: 0,
  })
  const first = await authenticate()
  first.disconnect()
  const second = await authenticate(1)
  assert.deepEqual(first.sent[1]?.params, input)
  assert.deepEqual(second.sent[1]?.params, input)
  acknowledge(second, { runId: secret })
  complete(second)
  assert.deepEqual(await started, { runId: secret })
})

function rejectShortToken(socket: FakeSocket) {
  const request = socket.sent.find(
    (frame) => frame.method !== "auth.authenticate"
  )!
  socket.receive({
    jsonrpc: "2.0",
    id: request.id,
    error: {
      code: -32010,
      message: "Your sign-in expires too soon for this run.",
      data: { code: "run_token_too_short" },
    },
  })
}

test("a short execution credential refreshes once and retries the unchanged submission", async () => {
  const { refreshAccessToken, expireSession } = setup()
  const input = {
    sessionId: "chat",
    turnId: "turn",
    clientRequestId: "request",
    content: "Hello",
    modelId: "openai:model",
    budgetPreset: "longer" as const,
  }
  const started = streamRun(input)
  const first = await authenticate()
  rejectShortToken(first)
  const second = await authenticate(1)
  assert.equal(first.closed, true)
  assert.equal(second.sent[0]?.params.accessToken, "refreshed-token")
  assert.deepEqual(first.sent[1]?.params, input)
  assert.deepEqual(second.sent[1]?.params, input)
  acknowledge(second, { runId: secret })
  complete(second)
  await started
  assert.equal(refreshAccessToken.mock.calls.length, 1)
  assert.equal(expireSession.mock.calls.length, 0)
})

test("a refreshed credential that is still short rejects without refreshing again or signing out", async () => {
  const { refreshAccessToken, expireSession } = setup()
  const started = streamRun({
    sessionId: "chat",
    turnId: "turn",
    clientRequestId: "request",
    content: "Hello",
  })
  const failed = assert.rejects(
    started,
    (error: unknown) =>
      error instanceof Error &&
      "code" in error &&
      error.code === "run_token_too_short"
  )
  rejectShortToken(await authenticate())
  rejectShortToken(await authenticate(1))
  await failed
  assert.equal(refreshAccessToken.mock.calls.length, 1)
  assert.equal(expireSession.mock.calls.length, 0)
  assert.equal(sockets.length, 2)
})

test("temporary refresh failure preserves sign-in and does not resubmit", async () => {
  const { refreshAccessToken, expireSession } = setup()
  refreshAccessToken.mockImplementation(async () => {
    throw new AuthAccessError("auth_unavailable", "Try again.")
  })
  const started = streamRun({
    sessionId: "chat",
    turnId: "turn",
    clientRequestId: "request",
    content: "Hello",
  })
  const failed = assert.rejects(
    started,
    (error: unknown) =>
      error instanceof Error &&
      "code" in error &&
      error.code === "auth_unavailable"
  )
  rejectShortToken(await authenticate())
  await failed
  assert.equal(refreshAccessToken.mock.calls.length, 1)
  assert.equal(expireSession.mock.calls.length, 0)
  assert.equal(sockets.length, 1)
})

test("reconnect exposes recovery policy and original limits without starting another run", async () => {
  setup()
  const infos: unknown[] = []
  const resumed = resumeRun(secret, { onRunInfo: (info) => infos.push(info) })
  const socket = await authenticate()
  assert.equal(socket.sent[1]?.method, "run.resume")
  const saved = {
    runId: secret,
    status: "recovering",
    modelId: "original-model",
    budgetPreset: "longer",
    retryAllowed: false,
    recoveryReason: "command_outcome_unknown",
    uncertainToolCallId: "tool-original",
    limits: {
      maxModelCalls: 12,
      maxToolCalls: 24,
      maxTotalTokens: 250000,
      maxCostUsd: 4,
      deadlineAt: "2026-10-23T01:00:00Z",
    },
  }
  acknowledge(socket, saved)
  await until(() => infos.length === 1)
  assert.deepEqual(infos, [saved])
  complete(socket)
  await resumed
  assert.equal(
    socket.sent.some((request) => request.method === "run.start"),
    false
  )
})
