import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  cancelRun,
  mergeRunEvents,
  resumeRun,
  respondToToolApproval,
  streamRun,
} from "@/lib/runtime-client"

const auth = vi.hoisted(() => ({
  getAccessToken: vi.fn(),
  getSnapshot: vi.fn(),
  subscribe: vi.fn(),
}))
const authListeners = new Set<() => void>()

vi.mock("@/lib/auth-controller", () => ({
  getAuthController: () => auth,
}))

it("keeps newer live events when saved history overlaps a running stream", () => {
  const event = (sequence: number, eventType: string) => ({
    runId: "run-1",
    sequence,
    eventType,
    eventVersion: 1,
    data: {},
  })
  expect(
    mergeRunEvents(
      "run-1",
      [event(1, "run.queued"), event(2, "run.started")],
      [
        { ...event(4, "run.failed"), runId: "another-attempt" },
        event(2, "run.started"),
        event(3, "tool.call"),
      ]
    ).map((item) => item.sequence)
  ).toEqual([1, 2, 3])
})

type SocketMessage = { data: string }

class MockWebSocket {
  static instances: MockWebSocket[] = []
  static onSend: (socket: MockWebSocket, payload: unknown) => void = () => {}
  static autoAuthenticate = true
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((event: SocketMessage) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  sent: unknown[] = []
  readonly url: string

  constructor(url: string) {
    this.url = url
    MockWebSocket.instances.push(this)
    queueMicrotask(() => {
      this.readyState = MockWebSocket.OPEN
      this.onopen?.()
    })
  }

  send(data: string) {
    const payload = JSON.parse(data) as unknown
    this.sent.push(payload)
    const request = rpcRequest(payload)
    if (
      request.method === "auth.authenticate" &&
      MockWebSocket.autoAuthenticate
    ) {
      this.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { userId: "user-1" },
      })
      return
    }
    MockWebSocket.onSend(this, payload)
  }

  close() {
    this.readyState = 3
    this.onclose?.()
  }

  reply(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }
}

function rpcRequest(payload: unknown) {
  if (Array.isArray(payload)) return payload[0] as Record<string, unknown>
  return payload as Record<string, unknown>
}

function event(
  runId: string,
  sequence: number,
  eventType: string,
  data: object
) {
  return {
    jsonrpc: "2.0",
    method: "run.event",
    params: { runId, sequence, eventType, eventVersion: 1, data },
  }
}

beforeEach(() => {
  auth.getAccessToken.mockResolvedValue({
    token: "test-token",
    userId: "user-1",
  })
  auth.getSnapshot.mockReturnValue({
    status: "signed-in",
    user: { id: "user-1" },
  })
  auth.subscribe.mockImplementation((listener: () => void) => {
    authListeners.add(listener)
    return () => authListeners.delete(listener)
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetAllMocks()
  authListeners.clear()
  MockWebSocket.instances = []
  MockWebSocket.onSend = () => {}
  MockWebSocket.autoAuthenticate = true
})

describe("streamRun", () => {
  it("uses a refreshed token when a run socket finishes opening", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    auth.getAccessToken
      .mockResolvedValueOnce({ token: "opening-token", userId: "user-1" })
      .mockResolvedValueOnce({ token: "fresh-token", userId: "user-1" })
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      if (request.method !== "run.start") return
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-fresh", status: "running", lastSequence: 0 },
      })
      socket.reply(event("run-fresh", 1, "run.completed", {}))
    }

    await streamRun({
      sessionId: "session-1",
      turnId: "turn-fresh",
      clientRequestId: "request-fresh",
      content: "Hello",
    })

    expect(MockWebSocket.instances[0]?.sent[0]).toMatchObject({
      method: "auth.authenticate",
      params: { accessToken: "fresh-token" },
    })
  })

  it("waits for the auth response before sending a run request", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.autoAuthenticate = false
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      if (request.method !== "run.start") return
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-wait", status: "running", lastSequence: 0 },
      })
      socket.reply(event("run-wait", 1, "run.completed", {}))
    }
    const running = streamRun({
      sessionId: "session-1",
      turnId: "turn-wait",
      clientRequestId: "request-wait",
      content: "Hello",
    })
    await vi.waitFor(() =>
      expect(MockWebSocket.instances[0]?.sent).toHaveLength(1)
    )
    const socket = MockWebSocket.instances[0]!
    const authRequest = rpcRequest(socket.sent[0])
    expect(authRequest.method).toBe("auth.authenticate")
    socket.reply({
      jsonrpc: "2.0",
      id: authRequest.id,
      result: { userId: "user-1" },
    })

    await expect(running).resolves.toEqual({ runId: "run-wait" })
    expect(socket.sent[1]).toMatchObject({ method: "run.start" })
  })

  it("stops without retrying when the server denies authentication", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.autoAuthenticate = false
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        error: { code: -32001, message: "Private auth detail" },
      })
    }

    await expect(
      streamRun(
        {
          sessionId: "session-1",
          turnId: "turn-denied",
          clientRequestId: "request-denied",
          content: "Hello",
        },
        { reconnectAttempts: 4 }
      )
    ).rejects.toMatchObject({
      code: "authentication_required",
      message: "Sign in to continue.",
    })
    expect(MockWebSocket.instances).toHaveLength(1)
    expect(MockWebSocket.instances[0]?.sent).toHaveLength(1)
  })

  it("does not open a socket when no verified token is available", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    auth.getAccessToken.mockRejectedValue(new Error("No session"))

    await expect(
      streamRun({
        sessionId: "session-1",
        turnId: "turn-no-auth",
        clientRequestId: "request-no-auth",
        content: "Hello",
      })
    ).rejects.toMatchObject({ code: "authentication_required" })
    expect(MockWebSocket.instances).toHaveLength(0)
  })

  it("stops an active run connection when the account signs out", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-switch", status: "running", lastSequence: 0 },
      })
    }
    const running = streamRun(
      {
        sessionId: "session-1",
        turnId: "turn-switch",
        clientRequestId: "request-switch",
        content: "Hello",
      },
      { reconnectAttempts: 0 }
    )
    await vi.waitFor(() =>
      expect(MockWebSocket.instances[0]?.sent).toHaveLength(2)
    )

    auth.getSnapshot.mockReturnValue({ status: "signed-out", user: null })
    for (const listener of authListeners) listener()
    const outcome = await Promise.race([
      running.then(
        () => "resolved",
        (error: unknown) => error
      ),
      new Promise<string>((resolve) =>
        setTimeout(() => resolve("pending"), 100)
      ),
    ])
    MockWebSocket.instances[0]?.close()

    expect(outcome).toMatchObject({ code: "authentication_required" })
    expect(MockWebSocket.instances[0]?.readyState).toBe(3)
  })

  it("authenticates before starting a run without putting the token in the URL", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-auth", status: "running", lastSequence: 0 },
      })
      socket.reply(event("run-auth", 1, "run.completed", {}))
    }

    await streamRun({
      sessionId: "session-1",
      turnId: "turn-auth",
      clientRequestId: "request-auth",
      content: "Hello",
    })

    const socket = MockWebSocket.instances[0]
    expect(socket?.sent[0]).toMatchObject({
      method: "auth.authenticate",
      params: { accessToken: "test-token" },
    })
    expect(socket?.sent[1]).toMatchObject({ method: "run.start" })
    expect(socket?.url).not.toContain("test-token")
  })

  it("reports the selected run limits from the start response", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-budget",
          status: "running",
          lastSequence: 0,
          budgetPreset: "conservative",
          limits: {
            maxModelCalls: 8,
            maxToolCalls: 16,
            maxTotalTokens: 100000,
            maxCostUsd: 2,
            deadlineAt: "2026-10-07T12:10:00Z",
          },
        },
      })
      socket.reply(event("run-budget", 1, "run.completed", {}))
    }
    const onRunInfo = vi.fn()

    await streamRun(
      {
        sessionId: "session-1",
        turnId: "turn-budget",
        clientRequestId: "request-budget",
        content: "Hello",
      },
      { onRunInfo }
    )

    expect(onRunInfo).toHaveBeenCalledWith({
      runId: "run-budget",
      budgetPreset: "conservative",
      limits: {
        maxModelCalls: 8,
        maxToolCalls: 16,
        maxTotalTokens: 100000,
        maxCostUsd: 2,
        deadlineAt: "2026-10-07T12:10:00Z",
      },
    })
  })

  it("starts a run and delivers ordered events through terminal completion", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      expect(request).toMatchObject({
        jsonrpc: "2.0",
        method: "run.start",
        params: {
          sessionId: "session-1",
          turnId: "turn-1",
          clientRequestId: "request-1",
          content: "Hello",
        },
      })
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-1", status: "running", lastSequence: 1 },
      })
      socket.reply(event("run-1", 1, "run.queued", { status: "queued" }))
      socket.reply(event("run-1", 2, "assistant.delta", { text: "Hi" }))
      socket.reply(event("run-1", 3, "run.completed", { status: "completed" }))
    }
    const onEvent = vi.fn()

    await expect(
      streamRun(
        {
          sessionId: "session-1",
          turnId: "turn-1",
          clientRequestId: "request-1",
          content: "Hello",
        },
        { onEvent }
      )
    ).resolves.toEqual({ runId: "run-1" })

    expect(onEvent.mock.calls.map(([runEvent]) => runEvent.eventType)).toEqual([
      "run.queued",
      "assistant.delta",
      "run.completed",
    ])
    expect(MockWebSocket.instances[0]?.url).toMatch(/^ws:\/\//)
  })

  it("requests durable replay when it detects an event sequence gap", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      if (request.method === "run.start") {
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: { runId: "run-2", status: "running", lastSequence: 1 },
        })
        socket.reply(event("run-2", 2, "assistant.delta", { text: "lost gap" }))
        return
      }
      expect(request).toMatchObject({
        method: "run.resume",
        params: { runId: "run-2", afterSequence: 0 },
      })
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-2", status: "running", lastSequence: 3 },
      })
      socket.reply(event("run-2", 1, "run.queued", { status: "queued" }))
      socket.reply(event("run-2", 2, "assistant.delta", { text: "A" }))
      socket.reply(event("run-2", 3, "assistant.delta", { text: "B" }))
      socket.reply(event("run-2", 4, "run.completed", { status: "completed" }))
    }
    const onEvent = vi.fn()

    await streamRun(
      {
        sessionId: "session-1",
        turnId: "turn-2",
        clientRequestId: "request-2",
        content: "Hello",
      },
      { onEvent }
    )

    expect(onEvent.mock.calls.map(([runEvent]) => runEvent.sequence)).toEqual([
      1, 2, 3, 4,
    ])
  })

  it("resumes a known run after disconnecting and ignores replayed events", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    auth.getAccessToken
      .mockResolvedValueOnce({ token: "first-token", userId: "user-1" })
      .mockResolvedValueOnce({ token: "first-token", userId: "user-1" })
      .mockResolvedValueOnce({ token: "refreshed-token", userId: "user-1" })
      .mockResolvedValueOnce({ token: "refreshed-token", userId: "user-1" })
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      if (request.method === "run.start") {
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: { runId: "run-3", status: "running", lastSequence: 0 },
        })
        socket.reply(event("run-3", 1, "assistant.delta", { text: "Hello" }))
        setTimeout(() => socket.close(), 0)
      } else {
        expect(request).toMatchObject({
          method: "run.resume",
          params: { runId: "run-3", afterSequence: 1 },
        })
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: { runId: "run-3", status: "running", lastSequence: 1 },
        })
        socket.reply(
          event("run-3", 1, "assistant.delta", { text: "duplicate" })
        )
        socket.reply(event("run-3", 2, "assistant.delta", { text: " world" }))
        socket.reply(
          event("run-3", 3, "run.completed", { status: "completed" })
        )
      }
    }
    const onEvent = vi.fn()

    await expect(
      streamRun(
        {
          sessionId: "session-1",
          turnId: "turn-3",
          clientRequestId: "request-3",
          content: "Hello",
        },
        { onEvent }
      )
    ).resolves.toEqual({ runId: "run-3" })

    expect(MockWebSocket.instances).toHaveLength(2)
    expect(MockWebSocket.instances[0]?.sent[0]).toMatchObject({
      params: { accessToken: "first-token" },
    })
    expect(MockWebSocket.instances[1]?.sent[0]).toMatchObject({
      params: { accessToken: "refreshed-token" },
    })
    expect(onEvent.mock.calls.map(([runEvent]) => runEvent.sequence)).toEqual([
      1, 2, 3,
    ])
  })

  it("surfaces a sanitized run failure", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-failed", status: "running", lastSequence: 0 },
      })
      socket.reply(
        event("run-failed", 1, "run.failed", {
          code: "provider_timeout",
          message: "The provider timed out.",
        })
      )
    }

    await expect(
      streamRun({
        sessionId: "session-1",
        turnId: "turn-failed",
        clientRequestId: "request-failed",
        content: "Hello",
      })
    ).rejects.toMatchObject({
      code: "provider_timeout",
      message: "The provider timed out.",
    })
  })
})

describe("resumeRun", () => {
  it("replays again when replay_required arrives before the resume response", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    let resumes = 0
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      expect(request).toMatchObject({
        method: "run.resume",
        params: { runId: "run-early-replay", afterSequence: 0 },
      })
      resumes += 1
      if (resumes === 1) {
        socket.reply({
          jsonrpc: "2.0",
          method: "run.replay_required",
          params: { runId: "run-early-replay", afterSequence: 0 },
        })
        socket.reply({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            runId: "run-early-replay",
            status: "running",
            lastSequence: 0,
          },
        })
        return
      }
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-early-replay",
          status: "running",
          lastSequence: 2,
        },
      })
      socket.reply(
        event("run-early-replay", 1, "run.queued", { status: "queued" })
      )
      socket.reply(
        event("run-early-replay", 2, "run.completed", { status: "completed" })
      )
    }
    const onEvent = vi.fn()

    await expect(resumeRun("run-early-replay", { onEvent })).resolves.toEqual({
      runId: "run-early-replay",
    })
    expect(resumes).toBe(2)
    expect(onEvent.mock.calls.map(([runEvent]) => runEvent.sequence)).toEqual([
      1, 2,
    ])
  })

  it("replays a discovered run from the beginning without starting another run", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      expect(request).toMatchObject({
        method: "run.resume",
        params: { runId: "run-restored", afterSequence: 0 },
      })
      socket.reply(event("run-restored", 1, "run.queued", { status: "queued" }))
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-restored", status: "running", lastSequence: 2 },
      })
      socket.reply(
        event("run-restored", 2, "assistant.delta", { text: "Restored" })
      )
      socket.reply(
        event("run-restored", 3, "run.completed", { status: "completed" })
      )
    }
    const onEvent = vi.fn()
    const onRunId = vi.fn()

    await expect(
      resumeRun("run-restored", { onEvent, onRunId })
    ).resolves.toEqual({ runId: "run-restored" })

    expect(onRunId).toHaveBeenCalledOnce()
    expect(onRunId).toHaveBeenCalledWith("run-restored")
    expect(onEvent.mock.calls.map(([runEvent]) => runEvent.sequence)).toEqual([
      1, 2, 3,
    ])
    expect(MockWebSocket.instances).toHaveLength(1)
  })

  it("resumes from the last delivered event after a later connection loss", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      expect(request).toMatchObject({
        method: "run.resume",
        params: { runId: "run-reconnect", afterSequence: 3 },
      })
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-reconnect", status: "running", lastSequence: 4 },
      })
      socket.reply(
        event("run-reconnect", 4, "assistant.delta", { text: "Done" })
      )
      socket.reply(
        event("run-reconnect", 5, "run.completed", { status: "completed" })
      )
    }
    const onEvent = vi.fn()

    await resumeRun("run-reconnect", { onEvent }, 3)

    expect(onEvent.mock.calls.map(([runEvent]) => runEvent.sequence)).toEqual([
      4, 5,
    ])
  })
})

describe("cancelRun", () => {
  it("uses a refreshed token when an action socket finishes opening", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    auth.getAccessToken
      .mockResolvedValueOnce({ token: "opening-token", userId: "user-1" })
      .mockResolvedValueOnce({ token: "fresh-token", userId: "user-1" })
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      if (request.method !== "run.cancel") return
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-fresh", status: "cancelling" },
      })
    }

    await cancelRun("run-fresh")

    expect(MockWebSocket.instances[0]?.sent[0]).toMatchObject({
      method: "auth.authenticate",
      params: { accessToken: "fresh-token" },
    })
  })

  it("does not repeat an action when auth replies are duplicated", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.autoAuthenticate = false
    const cancelling = cancelRun("run-stop", 0)
    await vi.waitFor(() =>
      expect(MockWebSocket.instances[0]?.sent).toHaveLength(1)
    )
    const socket = MockWebSocket.instances[0]!
    const authRequest = rpcRequest(socket.sent[0])
    const authReply = {
      jsonrpc: "2.0",
      id: authRequest.id,
      result: { userId: "user-1" },
    }
    socket.reply(authReply)
    await vi.waitFor(() => expect(socket.sent).toHaveLength(2))
    socket.reply(authReply)
    const action = rpcRequest(socket.sent[1])

    try {
      expect(
        socket.sent.filter((item) => rpcRequest(item).method === "run.cancel")
      ).toHaveLength(1)
    } finally {
      socket.reply({
        jsonrpc: "2.0",
        id: action.id,
        result: { runId: "run-stop", status: "cancelling" },
      })
      await cancelling
    }
  })

  it("closes a pending cancellation when the account signs out", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.autoAuthenticate = false
    const cancelling = cancelRun("run-stop", 0)
    await vi.waitFor(() =>
      expect(MockWebSocket.instances[0]?.sent).toHaveLength(1)
    )

    auth.getSnapshot.mockReturnValue({ status: "signed-out", user: null })
    for (const listener of authListeners) listener()
    const outcome = await Promise.race([
      cancelling.then(
        () => "resolved",
        (error: unknown) => error
      ),
      new Promise<string>((resolve) =>
        setTimeout(() => resolve("pending"), 100)
      ),
    ])
    MockWebSocket.instances[0]?.close()

    expect(outcome).toMatchObject({ code: "authentication_required" })
    expect(MockWebSocket.instances[0]?.sent).toHaveLength(1)
  })

  it("cancels a run at the last event cursor", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = payload as { id?: string }
      expect(payload).toMatchObject({
        jsonrpc: "2.0",
        method: "run.cancel",
        params: { runId: "run-stop", afterSequence: 7 },
      })
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: { runId: "run-stop", status: "cancelling", lastSequence: 8 },
      })
    }

    await expect(cancelRun("run-stop", 7)).resolves.toEqual({
      runId: "run-stop",
      status: "cancelling",
    })
    expect(MockWebSocket.instances[0]?.sent[0]).toMatchObject({
      method: "auth.authenticate",
      params: { accessToken: "test-token" },
    })
    expect(MockWebSocket.instances[0]?.sent[1]).toMatchObject({
      method: "run.cancel",
    })
  })
})

describe("respondToToolApproval", () => {
  it("answers the exact waiting call at the current event cursor", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      expect(request).toMatchObject({
        jsonrpc: "2.0",
        method: "run.respond",
        params: {
          runId: "run-edit",
          toolCallId: "tool-7",
          decision: "approved",
          afterSequence: 12,
        },
      })
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          runId: "run-edit",
          status: "waiting_for_approval",
          toolCallId: "tool-7",
          decision: "approved",
        },
      })
    }

    await expect(
      respondToToolApproval("run-edit", "tool-7", "approved", 12)
    ).resolves.toEqual({
      runId: "run-edit",
      toolCallId: "tool-7",
      decision: "approved",
    })
    expect(MockWebSocket.instances[0]?.sent[0]).toMatchObject({
      method: "auth.authenticate",
    })
    expect(MockWebSocket.instances[0]?.sent[1]).toMatchObject({
      method: "run.respond",
    })
  })

  it("surfaces a late decision as a retryable local error", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket)
    MockWebSocket.onSend = (socket, payload) => {
      const request = rpcRequest(payload)
      socket.reply({
        jsonrpc: "2.0",
        id: request.id,
        error: {
          code: -32000,
          message: "This tool call is not waiting for approval.",
          data: { code: "approval_not_pending" },
        },
      })
    }

    await expect(
      respondToToolApproval("run-edit", "tool-7", "denied", 12)
    ).rejects.toMatchObject({ code: "approval_not_pending" })
  })
})
