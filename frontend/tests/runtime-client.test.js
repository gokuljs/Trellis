import { afterEach, beforeEach, expect, mock, test } from "bun:test"

const originalWindow = globalThis.window
const originalWebSocket = globalThis.WebSocket
const originalSetTimeout = globalThis.setTimeout
const originalClearTimeout = globalThis.clearTimeout
const authModule = await import("../apps/web/src/lib/auth-controller.ts")
let credential
let state
let tokenRequest
const listeners = new Set()
const expired = []
const auth = {
  getSnapshot: () => state,
  getSessionEpoch: () => credential.sessionEpoch,
  getAccessToken: () => tokenRequest(),
  isCredentialCurrent: (candidate) =>
    state.status === "signed-in" &&
    candidate.userId === state.user.id &&
    candidate.sessionEpoch === credential.sessionEpoch,
  expireSession: async (candidate) => {
    if (
      !auth.isCredentialCurrent(candidate) ||
      candidate.token !== credential.token
    )
      return
    expired.push(candidate)
    state = { status: "signed-out", user: null }
    credential = { ...credential, sessionEpoch: credential.sessionEpoch + 1 }
    for (const listener of listeners) listener()
  },
  subscribe: (listener) => {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
}

function installAuth() {
  mock.module("../apps/web/src/lib/auth-controller.ts", () => ({
    ...authModule,
    getAuthController: () => auth,
  }))
}
installAuth()
const { cancelRun, resumeRun, respondToToolApproval, streamRun } =
  await import("../apps/web/src/lib/runtime-client.ts")

class FakeWebSocket {
  static sockets = []
  sent = []
  closed = false

  constructor(url) {
    this.url = url
    FakeWebSocket.sockets.push(this)
  }

  send(data) {
    if (this.closed) throw new Error("socket closed")
    this.sent.push(JSON.parse(data))
  }

  open() {
    this.onopen?.()
  }

  reply(id, result) {
    this.deliver({ jsonrpc: "2.0", id, result })
  }

  fail(id, code, status) {
    this.deliver({
      jsonrpc: "2.0",
      id,
      error: {
        code: -32001,
        message: "Private upstream detail",
        data: { code, status },
      },
    })
  }

  deliver(message) {
    this.onmessage?.({ data: JSON.stringify(message) })
  }

  close(code = 1000) {
    if (this.closed) return
    this.closed = true
    this.onclose?.({ code })
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function setAccount(
  userId,
  sessionEpoch,
  token = "test-token",
  notify = false
) {
  credential = { userId, sessionEpoch, token }
  state = { status: "signed-in", user: { id: userId } }
  if (notify) for (const listener of listeners) listener()
}

async function flush() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve()
}

function observe(promise) {
  const outcome = { value: null, error: null }
  void promise.then(
    (value) => {
      outcome.value = value
    },
    (error) => {
      outcome.error = error
    }
  )
  return outcome
}

function installTimers() {
  let nextId = 0
  const timers = new Map()
  globalThis.setTimeout = (callback, delay) => {
    const id = ++nextId
    timers.set(id, { callback, delay })
    return id
  }
  globalThis.clearTimeout = (id) => timers.delete(id)
  return {
    advance: (milliseconds) => {
      for (const [id, timer] of [...timers]) {
        if (timer.delay <= milliseconds && timers.delete(id)) timer.callback()
      }
    },
    pending: () => timers.size,
  }
}

async function authenticateSocket() {
  await flush()
  const socket = FakeWebSocket.sockets.at(-1)
  socket.open()
  await flush()
  const request = socket.sent.find(
    (message) => message.method === "auth.authenticate"
  )
  socket.reply(request.id, { userId: "account-a" })
  await flush()
  return socket
}

function runEvent(socket, sequence, eventType, runId = "run-a") {
  socket.deliver({
    jsonrpc: "2.0",
    method: "run.event",
    params: {
      runId,
      sequence,
      eventType,
      eventVersion: 1,
      data: {},
    },
  })
}

const input = {
  sessionId: "chat-a",
  turnId: "turn-a",
  clientRequestId: "request-a",
  content: "Hello",
}

beforeEach(() => {
  installAuth()
  listeners.clear()
  expired.length = 0
  FakeWebSocket.sockets = []
  setAccount("account-a", 1)
  tokenRequest = async () => credential
  globalThis.window = { location: { href: "https://trellis.example/chat" } }
  globalThis.WebSocket = FakeWebSocket
})

afterEach(async () => {
  state = { status: "signed-out", user: null }
  for (const listener of listeners) listener()
  for (const socket of FakeWebSocket.sockets) socket.close()
  await flush()
  globalThis.window = originalWindow
  globalThis.WebSocket = originalWebSocket
  globalThis.setTimeout = originalSetTimeout
  globalThis.clearTimeout = originalClearTimeout
})

test("returning to the same account during token retrieval cannot open a runtime socket", async () => {
  const token = deferred()
  tokenRequest = () => token.promise
  const outcome = observe(streamRun(input))
  setAccount("account-b", 2)
  setAccount("account-a", 3, "new-token")
  token.resolve(credential)
  await flush()
  expect(FakeWebSocket.sockets).toHaveLength(0)
  expect(outcome.error?.status).toBe(401)
  expect(expired).toHaveLength(0)
})

test("returning to the same account during socket opening cannot send a handshake", async () => {
  const outcome = observe(streamRun(input))
  await flush()
  const socket = FakeWebSocket.sockets[0]
  const token = deferred()
  tokenRequest = () => token.promise
  socket.open()
  setAccount("account-b", 2)
  setAccount("account-a", 3, "new-token")
  token.resolve(credential)
  await flush()
  expect(socket.sent).toHaveLength(0)
  expect(outcome.error?.status).toBe(401)
})

test("stale live events cannot publish after leaving and returning to the same account", async () => {
  const events = []
  const outcome = observe(
    streamRun(input, { onEvent: (event) => events.push(event) })
  )
  const socket = await authenticateSocket()
  socket.reply(
    socket.sent.find((message) => message.method === "run.start").id,
    { runId: "run-a" }
  )
  await flush()
  setAccount("account-b", 2)
  setAccount("account-a", 3, "new-token")
  runEvent(socket, 1, "run.completed")
  await flush()
  expect(events).toHaveLength(0)
  expect(outcome.error?.status).toBe(401)
  expect(outcome.value).toBeNull()
})

test("temporary handshake verification failures remain retryable and keep the session", async () => {
  const outcome = observe(streamRun(input))
  await flush()
  const socket = FakeWebSocket.sockets[0]
  socket.open()
  await flush()
  socket.fail(socket.sent[0].id, "auth_unavailable", 503)
  await flush()
  expect(outcome.error).toMatchObject({ code: "auth_unavailable", status: 503 })
  expect(outcome.error.message).not.toContain("Private upstream detail")
  expect(state.user.id).toBe("account-a")
  expect(expired).toHaveLength(0)
})

test("cancel cannot use a new session after a pending credential returns", async () => {
  const token = deferred()
  tokenRequest = () => token.promise
  const outcome = observe(cancelRun("run-a"))
  setAccount("account-b", 2)
  setAccount("account-a", 3, "new-token")
  token.resolve(credential)
  await flush()
  expect(FakeWebSocket.sockets).toHaveLength(0)
  expect(outcome.error?.status).toBe(401)
})

test("cancel cannot send a stale handshake after returning to the same account", async () => {
  const outcome = observe(cancelRun("run-a"))
  await flush()
  const socket = FakeWebSocket.sockets[0]
  const token = deferred()
  tokenRequest = () => token.promise
  socket.open()
  setAccount("account-b", 2)
  setAccount("account-a", 3, "new-token")
  token.resolve(credential)
  await flush()
  expect(socket.sent).toHaveLength(0)
  expect(outcome.error?.status).toBe(401)
})

test("a stale cancellation response cannot resolve for a new session", async () => {
  const outcome = observe(cancelRun("run-a"))
  const socket = await authenticateSocket()
  expect(socket.sent.at(-1).method).toBe("run.cancel")
  setAccount("account-b", 2)
  setAccount("account-a", 3, "new-token")
  socket.reply("runtime-cancel", { runId: "run-a", status: "cancelling" })
  await flush()
  expect(outcome.value).toBeNull()
  expect(outcome.error?.status).toBe(401)
  expect(expired).toHaveLength(0)
})

test("approval handshake outages remain retryable and do not expire the session", async () => {
  const outcome = observe(respondToToolApproval("run-a", "tool-a", "approved"))
  await flush()
  const socket = FakeWebSocket.sockets[0]
  socket.open()
  await flush()
  socket.fail("runtime-auth", "auth_unavailable", 503)
  await flush()
  expect(outcome.error).toMatchObject({ code: "auth_unavailable", status: 503 })
  expect(outcome.error.message).not.toContain("Private upstream detail")
  expect(state.user.id).toBe("account-a")
  expect(expired).toHaveLength(0)
})

for (const [name, start] of [
  ["stream", () => streamRun(input)],
  ["cancel", () => cancelRun("run-a")],
]) {
  test(`${name} keeps handshake token outages retryable`, async () => {
    const outcome = observe(start())
    await flush()
    const socket = FakeWebSocket.sockets[0]
    tokenRequest = async () => {
      throw new authModule.AuthAccessError(
        "auth_unavailable",
        "Private token detail"
      )
    }
    socket.open()
    await flush()
    expect(socket.sent).toHaveLength(0)
    expect(outcome.error).toMatchObject({
      code: "auth_unavailable",
      status: 503,
    })
    expect(state.user.id).toBe("account-a")
    expect(expired).toHaveLength(0)
  })

  test(`${name} ignores old handshake rejection after a session change`, async () => {
    const outcome = observe(start())
    await flush()
    const socket = FakeWebSocket.sockets[0]
    socket.open()
    await flush()
    setAccount("account-b", 2)
    setAccount("account-a", 3, "new-token")
    socket.fail(socket.sent[0].id, "auth_failed", 401)
    await flush()
    expect(outcome.error?.status).toBe(401)
    expect(socket.sent).toHaveLength(1)
    expect(state.user.id).toBe("account-a")
    expect(expired).toHaveLength(0)
  })

  test(`${name} bounds socket opening without expiring the session`, async () => {
    const timers = installTimers()
    const outcome = observe(start())
    await flush()
    expect(FakeWebSocket.sockets).toHaveLength(1)
    timers.advance(30_000)
    await flush()
    expect(outcome.error).toMatchObject({
      code: "runtime_timeout",
      status: 503,
    })
    expect(FakeWebSocket.sockets[0].closed).toBe(true)
    expect(timers.pending()).toBe(0)
    expect(state.user.id).toBe("account-a")
    expect(expired).toHaveLength(0)
  })

  test(`${name} bounds an unanswered authentication handshake`, async () => {
    const timers = installTimers()
    const outcome = observe(start())
    await flush()
    const socket = FakeWebSocket.sockets[0]
    socket.open()
    await flush()
    expect(socket.sent[0].method).toBe("auth.authenticate")
    timers.advance(30_000)
    await flush()
    expect(outcome.error).toMatchObject({
      code: "runtime_timeout",
      status: 503,
    })
    expect(timers.pending()).toBe(0)
    expect(expired).toHaveLength(0)
  })

  test(`${name} preserves temporary token retrieval failures`, async () => {
    tokenRequest = async () => {
      throw new authModule.AuthAccessError(
        "auth_unavailable",
        "Private token detail"
      )
    }
    const outcome = observe(start())
    await flush()
    expect(outcome.error).toMatchObject({
      code: "auth_unavailable",
      status: 503,
    })
    expect(outcome.error.message).not.toContain("Private token detail")
    expect(FakeWebSocket.sockets).toHaveLength(0)
    expect(state.user.id).toBe("account-a")
    expect(expired).toHaveLength(0)
  })

  test(`${name} preserves expired token retrieval failures`, async () => {
    tokenRequest = async () => {
      throw new authModule.AuthAccessError(
        "reauth_required",
        "Private token detail"
      )
    }
    const outcome = observe(start())
    await flush()
    expect(outcome.error).toMatchObject({
      code: "reauth_required",
      status: 401,
    })
    expect(outcome.error.message).not.toContain("Private token detail")
    expect(FakeWebSocket.sockets).toHaveLength(0)
  })

  test(`${name} expires only the current server-rejected handshake credential`, async () => {
    const outcome = observe(start())
    await flush()
    const socket = FakeWebSocket.sockets[0]
    socket.open()
    await flush()
    socket.fail(socket.sent[0].id, "auth_failed", 401)
    await flush()
    expect(outcome.error).toMatchObject({
      code: "reauth_required",
      status: 401,
    })
    expect(state.status).toBe("signed-out")
    expect(expired).toHaveLength(1)
  })

  test(`${name} keeps a refreshed token when an older handshake credential is rejected`, async () => {
    const outcome = observe(start())
    await flush()
    const socket = FakeWebSocket.sockets[0]
    socket.open()
    await flush()
    setAccount("account-a", 1, "refreshed-token")
    socket.fail(socket.sent[0].id, "auth_failed", 401)
    await flush()
    expect(outcome.error?.status).toBe(401)
    expect(state.user.id).toBe("account-a")
    expect(expired).toHaveLength(0)
  })

  for (const [closeCode, expectedCode, status] of [
    [4401, "reauth_required", 401],
    [1013, "auth_unavailable", 503],
  ]) {
    test(`${name} preserves authentication category from close ${closeCode}`, async () => {
      const outcome = observe(start())
      const socket = await authenticateSocket()
      socket.close(closeCode)
      await flush()
      expect(outcome.error).toMatchObject({ code: expectedCode, status })
      expect(expired).toHaveLength(closeCode === 4401 ? 1 : 0)
      expect(FakeWebSocket.sockets).toHaveLength(1)
    })
  }

  for (const [code, status] of [
    ["auth_unavailable", 503],
    ["database_unavailable", 503],
    ["account_in_use", 409],
    ["cloud_access_denied", 401],
    ["cloud_access_denied", 403],
  ]) {
    test(`${name} handles unsolicited ${code} ${status} failures before close`, async () => {
      const outcome = observe(start())
      const socket = await authenticateSocket()
      socket.fail(null, code, status)
      socket.close(status === 401 ? 4401 : 1013)
      await flush()
      expect(outcome.error).toMatchObject({ code, status })
      expect(expired).toHaveLength(status === 401 ? 1 : 0)
      expect(FakeWebSocket.sockets).toHaveLength(1)
    })
  }
}

test("cancel uses the action-specific safe fallback for incomplete error envelopes", async () => {
  const outcome = observe(cancelRun("run-a"))
  const socket = await authenticateSocket()
  socket.deliver({
    jsonrpc: "2.0",
    id: "runtime-cancel",
    error: { code: -32000 },
  })
  await flush()
  expect(outcome.error.message).toBe("Trellis could not cancel that run.")
})

test("reconnect resumes at the last event and ignores callbacks from the old socket", async () => {
  const events = []
  const outcome = observe(
    streamRun(input, {
      reconnectDelayMs: 0,
      onEvent: (event) => events.push(event),
    })
  )
  const first = await authenticateSocket()
  first.reply(first.sent.find((message) => message.method === "run.start").id, {
    runId: "run-a",
  })
  await flush()
  runEvent(first, 1, "run.started")
  first.close(1006)
  await new Promise((resolve) => setTimeout(resolve, 1))
  const second = await authenticateSocket()
  const resume = second.sent.find((message) => message.method === "run.resume")
  expect(resume.params).toEqual({ runId: "run-a", afterSequence: 1 })
  first.fail(null, "auth_failed", 401)
  runEvent(first, 2, "run.failed")
  second.reply(resume.id, { runId: "run-a" })
  await flush()
  runEvent(second, 2, "run.completed")
  await flush()
  expect(outcome.value).toEqual({ runId: "run-a" })
  expect(events.map((event) => event.eventType)).toEqual([
    "run.started",
    "run.completed",
  ])
  expect(expired).toHaveLength(0)
})

test("cancel and approval actions keep their existing public results", async () => {
  const cancelled = observe(cancelRun("run-a", 7))
  const cancelSocket = await authenticateSocket()
  expect(cancelSocket.sent.at(-1).params).toEqual({
    runId: "run-a",
    afterSequence: 7,
  })
  cancelSocket.reply("runtime-cancel", { runId: "run-a", status: "cancelling" })
  await flush()
  expect(cancelled.value).toEqual({ runId: "run-a", status: "cancelling" })

  const approved = observe(
    respondToToolApproval("run-a", "tool-a", "approved", 9)
  )
  const approvalSocket = await authenticateSocket()
  expect(approvalSocket.sent.at(-1).params).toEqual({
    runId: "run-a",
    toolCallId: "tool-a",
    decision: "approved",
    afterSequence: 9,
  })
  approvalSocket.reply("runtime-respond", {
    runId: "run-a",
    toolCallId: "tool-a",
    decision: "approved",
  })
  await flush()
  expect(approved.value).toEqual({
    runId: "run-a",
    toolCallId: "tool-a",
    decision: "approved",
  })
})

test("resume publishes the saved cursor without starting a second run", async () => {
  const outcome = observe(resumeRun("run-a", {}, 4))
  const socket = await authenticateSocket()
  expect(socket.sent.at(-1)).toMatchObject({
    method: "run.resume",
    params: { runId: "run-a", afterSequence: 4 },
  })
  socket.reply(socket.sent.at(-1).id, { runId: "run-a" })
  await flush()
  runEvent(socket, 5, "run.completed")
  await flush()
  expect(outcome.value).toEqual({ runId: "run-a" })
})

test("the run acknowledgement exposes its locked model to context displays", async () => {
  const infos = []
  const outcome = observe(
    streamRun(input, { onRunInfo: (info) => infos.push(info) })
  )
  const socket = await authenticateSocket()
  const request = socket.sent.find((message) => message.method === "run.start")
  socket.reply(request.id, { runId: "run-a", modelId: "locked-model" })
  await flush()
  runEvent(socket, 1, "run.completed")
  await flush()
  expect(infos[0].modelId).toBe("locked-model")
  expect(outcome.value).toEqual({ runId: "run-a" })
})
