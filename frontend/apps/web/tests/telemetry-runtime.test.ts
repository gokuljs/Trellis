import assert from "node:assert/strict"
import { afterEach, test, mock, spyOn } from "bun:test"
import * as Sentry from "@sentry/react"

import * as authModule from "../src/lib/auth-controller"
import type { AuthController } from "../src/lib/auth-controller"
import {
  cancelRun,
  respondToToolApproval,
  resumeRun,
  streamRun,
} from "../src/lib/runtime-client"
import { createTelemetryOptions, reportFailure } from "../src/lib/telemetry"
import { parseTelemetryConfig } from "../src/lib/telemetry-policy"
import { isReconciledRunOutcome, ownsRun } from "../src/lib/run-recovery"

type Envelope = Parameters<
  ReturnType<NonNullable<Sentry.BrowserOptions["transport"]>>["send"]
>[0]
const secret = "private-runtime-token-prompt-session-tool"
const originalSocket = globalThis.WebSocket
const originalWindow = globalThis.window
const nativeSetTimeout = globalThis.setTimeout
const nativeClearTimeout = globalThis.clearTimeout
const sockets: FakeSocket[] = []
const deadlines = new Map<ReturnType<typeof setTimeout>, () => void>()

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

afterEach(async () => {
  for (const socket of sockets.splice(0)) socket.close()
  await Sentry.close(1000)
  Sentry.setCurrentClient(undefined)
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
  const credential = { token: secret, userId: secret, sessionEpoch: epoch }
  const auth = {
    getSnapshot: () => ({ status: "signed-in", user: { id: secret } }),
    getSessionEpoch: () => epoch,
    getAccessToken: async () => credential,
    isCredentialCurrent: () => epoch === credential.sessionEpoch,
    expireSession: async () => undefined,
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
  const envelopes: Envelope[] = []
  const client = Sentry.init({
    ...createTelemetryOptions(
      parseTelemetryConfig({
        VITE_SENTRY_ENABLED: "true",
        VITE_SENTRY_DSN: "https://public@o123.ingest.sentry.io/456",
        VITE_SENTRY_TRACES_SAMPLE_RATE: "1",
        origin: "https://trellis.test",
      })
    ),
    transport: () => ({
      send: (envelope) => {
        envelopes.push(envelope)
        return Promise.resolve({ statusCode: 200 })
      },
      flush: () => Promise.resolve(true),
    }),
  })!
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
    changeAccount() {
      epoch++
      for (const listener of listeners) listener()
    },
    async recorded() {
      await client.flush(1000)
      assert.equal(JSON.stringify(envelopes).includes(secret), false)
      const events = envelopes.flatMap((envelope) =>
        envelope[1].filter(([header]) => header.type === "event")
      )
      const spans = envelopes.flatMap((envelope) =>
        envelope[1].flatMap(([header, payload]) =>
          header.type === "span"
            ? (
                payload as {
                  items: {
                    name: string
                    status: string
                    end_timestamp?: number
                  }[]
                }
              ).items
            : []
        )
      )
      for (const span of spans)
        assert.equal(typeof span.end_timestamp, "number")
      return { events, spans }
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

test("start and subscribe spans end at acknowledgement before the run finishes", async () => {
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
  const beforeCompletion = await recorded()
  assert.deepEqual(
    beforeCompletion.spans.map((span) => [span.name, span.status]),
    [["run.start", "ok"]]
  )
  complete(first)
  await started
  const resumed = resumeRun(secret)
  const second = await authenticate(1)
  assert.equal(second.sent[1]?.method, "run.resume")
  acknowledge(second, { runId: secret })
  complete(second)
  await resumed
  assert.deepEqual(
    (await recorded()).spans.map((span) => span.name),
    ["run.start", "run.subscribe"]
  )
  assert.equal((await recorded()).events.length, 0)
})

test("cancel and respond acknowledgements validate results before ending spans", async () => {
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
  assert.deepEqual(
    (await recorded()).spans.map((span) => [span.name, span.status]),
    [
      ["run.cancel", "ok"],
      ["run.respond", "error"],
    ]
  )
  assert.equal((await recorded()).events.length, 0)
})

test("timeout and account teardown settle pending acknowledgement spans", async () => {
  const { recorded, changeAccount } = setup()
  const timed = cancelRun(secret)
  await authenticate()
  for (const handler of [...deadlines.values()]) handler()
  await assert.rejects(timed)
  const stale = resumeRun(secret)
  await authenticate(1)
  changeAccount()
  await assert.rejects(stale)
  assert.deepEqual(
    (await recorded()).spans.map((span) => [span.name, span.status]),
    [
      ["run.cancel", "error"],
      ["run.subscribe", "ok"],
    ]
  )
  assert.equal((await recorded()).events.length, 0)
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
  assert.equal((await recorded()).events.length, 0)
  const exhausted = resumeRun(secret, { reconnectAttempts: 0 })
  const third = await authenticate(2)
  third.disconnect()
  await assert.rejects(exhausted, (error) => {
    reportFailure(error, "run.restore")
    reportFailure(error, "run.restore")
    return true
  })
  assert.equal((await recorded()).events.length, 1)
})

test("invalid start acknowledgements and mismatched cancellation end spans as failures", async () => {
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
  assert.deepEqual(
    (await recorded()).spans.map((span) => [span.name, span.status]),
    [
      ["run.start", "error"],
      ["run.cancel", "error"],
      ["run.subscribe", "error"],
    ]
  )
  assert.equal((await recorded()).events.length, 0)
})

test("RPC rejection settles a span while successful approval stays quiet", async () => {
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
  assert.deepEqual(
    (await recorded()).spans.map((span) => [span.name, span.status]),
    [
      ["run.respond", "error"],
      ["run.respond", "ok"],
    ]
  )
  assert.equal((await recorded()).events.length, 0)
})

test("reconciliation policy accepts completed and expected terminal outcomes and rejects stale owners", async () => {
  const { recorded } = setup()
  for (const status of ["completed", "cancelled", "interrupted"]) {
    assert.equal(isReconciledRunOutcome(status), true)
  }
  for (const status of ["running", "queued", "failed", null, undefined]) {
    assert.equal(isReconciledRunOutcome(status), false)
  }
  const owner = { sessionId: secret, generation: 1 }
  const current = { sessionId: "new-session", generation: 2 }
  if (ownsRun(owner, current)) reportFailure(new Error(secret), "run.restore")
  assert.equal((await recorded()).events.length, 0)
})
