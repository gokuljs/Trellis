import { afterEach, describe, expect, it, vi } from "vitest"

import {
  cancelRun,
  respondToToolApproval,
  streamRun,
} from "@/lib/runtime-client"

type SocketMessage = { data: string }

class MockWebSocket {
  static instances: MockWebSocket[] = []
  static onSend: (socket: MockWebSocket, payload: unknown) => void = () => {}
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

afterEach(() => {
  vi.unstubAllGlobals()
  MockWebSocket.instances = []
  MockWebSocket.onSend = () => {}
})

describe("streamRun", () => {
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

describe("cancelRun", () => {
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
