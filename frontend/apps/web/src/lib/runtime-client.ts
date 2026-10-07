import type { RunSummary } from "@/lib/app-types"

export type RuntimeRunEvent = {
  runId: string
  sequence: number
  eventType: string
  eventVersion: number
  data: Record<string, unknown>
  createdAt?: string
}

export type RuntimeRunInfo = {
  runId: string
  budgetPreset?: string
  limits?: {
    maxModelCalls: number
    maxToolCalls: number
    maxTotalTokens: number
    maxCostUsd: number
    deadlineAt: string
  }
}

export type TurnRunActivity = {
  events: RuntimeRunEvent[]
  runInfo: RuntimeRunInfo | null
  latestEvent?: RuntimeRunEvent
  summary?: RunSummary
  priorAttempts?: RunAttemptActivity[]
  eventsLoading?: boolean
  eventsError?: string | null
}

export type RunAttemptActivity = {
  runId: string
  summary: RunSummary | null
  runInfo: RuntimeRunInfo | null
  events: RuntimeRunEvent[] | null
  loading?: boolean
  error?: string | null
}

export function mergeRunEvents(
  runId: string,
  saved: RuntimeRunEvent[],
  live: RuntimeRunEvent[]
): RuntimeRunEvent[] {
  const bySequence = new Map(
    saved
      .filter((event) => event.runId === runId)
      .map((event) => [event.sequence, event])
  )
  for (const event of live) {
    if (event.runId === runId) bySequence.set(event.sequence, event)
  }
  return [...bySequence.values()].sort(
    (left, right) => left.sequence - right.sequence
  )
}

export type ToolApprovalDecision = "approved" | "denied"

export type StartRunInput = {
  sessionId: string
  turnId: string
  clientRequestId: string
  content: string
  modelId?: string
  budgetPreset?: "conservative" | "longer"
}

export type RuntimeErrorPayload = {
  code: string
  message: string
}

export class RuntimeError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.code = code
    this.name = "RuntimeError"
  }
}

type RpcResponse = {
  jsonrpc: "2.0"
  id: string
  result?: Record<string, unknown>
  error?: { code: number; message: string; data?: { code?: string } }
}

type RpcRequest = {
  jsonrpc: "2.0"
  id: string
  method: string
  params: Record<string, unknown>
}

type RuntimeClientOptions = {
  onEvent?: (event: RuntimeRunEvent) => void
  onRunId?: (runId: string) => void
  onRunInfo?: (run: RuntimeRunInfo) => void
  reconnectAttempts?: number
  reconnectDelayMs?: number
}

type RunResult = { runId: string }

const rpcTimeoutMs = 30_000
const maxBufferedEvents = 500

function runtimeUrl() {
  const url = new URL("/api/runtime", window.location.href)
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
  return url.toString()
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function failureFromEvent(event: RuntimeRunEvent): RuntimeError {
  const code = event.data.code
  const message = event.data.message
  return new RuntimeError(
    typeof code === "string" ? code : "run_failed",
    typeof message === "string"
      ? message
      : "Trellis could not complete that run."
  )
}

function parseRunEvent(
  payload: Record<string, unknown>
): RuntimeRunEvent | null {
  const runId = payload.runId
  const sequence = payload.sequence
  const eventType = payload.eventType
  const eventVersion = payload.eventVersion
  const data = asRecord(payload.data)
  if (
    typeof runId !== "string" ||
    typeof sequence !== "number" ||
    !Number.isSafeInteger(sequence) ||
    typeof eventType !== "string" ||
    typeof eventVersion !== "number" ||
    !data
  ) {
    return null
  }
  return {
    runId,
    sequence,
    eventType,
    eventVersion,
    data,
    ...(typeof payload.createdAt === "string"
      ? { createdAt: payload.createdAt }
      : {}),
  }
}

function rpcError(response: RpcResponse): RuntimeError {
  const code = response.error?.data?.code
  return new RuntimeError(
    typeof code === "string" ? code : "runtime_request_failed",
    response.error?.message ?? "Trellis could not start that run."
  )
}

function parseRunInfo(result: Record<string, unknown>): RuntimeRunInfo {
  const runId = result.runId as string
  const info: RuntimeRunInfo = { runId }
  if (typeof result.budgetPreset === "string") {
    info.budgetPreset = result.budgetPreset
  }
  const limits = asRecord(result.limits)
  if (
    limits &&
    typeof limits.maxModelCalls === "number" &&
    typeof limits.maxToolCalls === "number" &&
    typeof limits.maxTotalTokens === "number" &&
    typeof limits.maxCostUsd === "number" &&
    typeof limits.deadlineAt === "string"
  ) {
    info.limits = {
      maxModelCalls: limits.maxModelCalls,
      maxToolCalls: limits.maxToolCalls,
      maxTotalTokens: limits.maxTotalTokens,
      maxCostUsd: limits.maxCostUsd,
      deadlineAt: limits.deadlineAt,
    }
  }
  return info
}

function followRun(
  input: StartRunInput | null,
  resumedRunId: string | null,
  options: RuntimeClientOptions,
  initialSequence: number
): Promise<RunResult> {
  const reconnectLimit = options.reconnectAttempts ?? 4
  const reconnectDelay = options.reconnectDelayMs ?? 200

  return new Promise((resolve, reject) => {
    let socket: WebSocket | null = null
    let runId: string | null = resumedRunId
    let lastSequence = initialSequence
    let runIdNotified = false
    let subscriptionReady = false
    let reconnects = 0
    let nextRpcId = 0
    let settled = false
    let subscriptionRequestPending = false
    let bufferedEventOverflowed = false
    let replayRequiredForRun: string | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    const pending = new Map<
      string,
      { resolve: (value: RpcResponse) => void; reject: (reason: Error) => void }
    >()
    const bufferedEvents: RuntimeRunEvent[] = []

    const finish = (error?: Error) => {
      if (settled) return
      settled = true
      if (reconnectTimer !== null) clearTimeout(reconnectTimer)
      for (const request of pending.values()) {
        request.reject(
          error ?? new RuntimeError("run_finished", "Run finished.")
        )
      }
      pending.clear()
      socket?.close()
      if (error) reject(error)
      else if (runId) resolve({ runId })
      else
        reject(
          new RuntimeError("runtime_protocol_error", "Run ended without an ID.")
        )
    }

    const processEvent = (event: RuntimeRunEvent) => {
      if (settled || !runId || event.runId !== runId) return
      if (event.sequence <= lastSequence) return
      if (event.sequence !== lastSequence + 1) {
        if (socket) beginRequest(socket)
        return
      }
      lastSequence = event.sequence
      options.onEvent?.(event)
      if (event.eventType === "run.completed") {
        finish()
      } else if (
        event.eventType === "run.failed" ||
        event.eventType === "run.cancelled" ||
        event.eventType === "run.interrupted"
      ) {
        finish(failureFromEvent(event))
      }
    }

    const flushBufferedEvents = () => {
      if (bufferedEventOverflowed) {
        bufferedEventOverflowed = false
        bufferedEvents.length = 0
        if (socket) beginRequest(socket)
        return
      }
      for (const event of bufferedEvents.splice(0)) processEvent(event)
    }

    const sendRpc = (
      activeSocket: WebSocket,
      method: string,
      params: Record<string, unknown>
    ) => {
      const id = `runtime-${++nextRpcId}`
      const request: RpcRequest = { jsonrpc: "2.0", id, method, params }
      return new Promise<RpcResponse>((resolveRpc, rejectRpc) => {
        const timer = setTimeout(() => {
          pending.delete(id)
          rejectRpc(
            new RuntimeError(
              "runtime_timeout",
              "The local service did not respond."
            )
          )
        }, rpcTimeoutMs)
        pending.set(id, {
          resolve: (response) => {
            clearTimeout(timer)
            resolveRpc(response)
          },
          reject: (error) => {
            clearTimeout(timer)
            rejectRpc(error)
          },
        })
        try {
          activeSocket.send(JSON.stringify(request))
        } catch {
          pending.delete(id)
          clearTimeout(timer)
          rejectRpc(
            new RuntimeError(
              "connection_lost",
              "Connection to Trellis was lost."
            )
          )
        }
      })
    }

    const beginRequest = (activeSocket: WebSocket) => {
      if (subscriptionRequestPending) return
      subscriptionRequestPending = true
      const method = runId ? "run.resume" : "run.start"
      const params = runId ? { runId, afterSequence: lastSequence } : input
      if (!params) {
        finish(
          new RuntimeError(
            "runtime_protocol_error",
            "Run cannot start without its request."
          )
        )
        return
      }
      void sendRpc(activeSocket, method, params)
        .then((response) => {
          if (settled || socket !== activeSocket) return
          subscriptionRequestPending = false
          if (response.error) throw rpcError(response)
          const result = response.result
          const responseRunId = result?.runId
          if (!result || typeof responseRunId !== "string") {
            throw new RuntimeError(
              "runtime_protocol_error",
              "The local service returned an invalid run."
            )
          }
          if (runId && responseRunId !== runId) {
            throw new RuntimeError(
              "runtime_protocol_error",
              "The local service resumed a different run."
            )
          }
          if (!runId) runId = responseRunId
          if (!runIdNotified) {
            runIdNotified = true
            options.onRunId?.(runId)
          }
          options.onRunInfo?.(parseRunInfo(result))
          subscriptionReady = true
          flushBufferedEvents()
          if (!settled && replayRequiredForRun === runId) {
            replayRequiredForRun = null
            beginRequest(activeSocket)
          }
        })
        .catch((error: unknown) => {
          if (settled || socket !== activeSocket) return
          subscriptionRequestPending = false
          const runtimeError =
            error instanceof RuntimeError
              ? error
              : new RuntimeError(
                  "runtime_request_failed",
                  "Trellis could not start that run."
                )
          finish(runtimeError)
        })
    }

    const scheduleReconnect = () => {
      if (settled || reconnectTimer !== null) return
      if (reconnects >= reconnectLimit) {
        finish(
          new RuntimeError(
            "connection_lost",
            "Connection to Trellis was lost while the run was active."
          )
        )
        return
      }
      reconnects += 1
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        connect()
      }, reconnectDelay * reconnects)
    }

    const handleMessage = (activeSocket: WebSocket, raw: unknown) => {
      if (typeof raw !== "string") return
      let decoded: unknown
      try {
        decoded = JSON.parse(raw) as unknown
      } catch {
        finish(
          new RuntimeError(
            "runtime_protocol_error",
            "The local service sent invalid data."
          )
        )
        return
      }
      const messages = Array.isArray(decoded) ? decoded : [decoded]
      for (const value of messages) {
        const message = asRecord(value)
        if (!message || message.jsonrpc !== "2.0") continue
        if (typeof message.id === "string") {
          const request = pending.get(message.id)
          if (request) {
            pending.delete(message.id)
            request.resolve(message as unknown as RpcResponse)
          }
          continue
        }
        if (message.method === "run.event") {
          const params = asRecord(message.params)
          const event = params ? parseRunEvent(params) : null
          if (!event) continue
          if (!subscriptionReady) {
            if (bufferedEvents.length < maxBufferedEvents)
              bufferedEvents.push(event)
            else bufferedEventOverflowed = true
          } else {
            processEvent(event)
          }
        } else if (message.method === "run.replay_required") {
          const params = asRecord(message.params)
          const requestedRunId = params?.runId
          if (
            typeof requestedRunId === "string" &&
            (!runId || requestedRunId === runId) &&
            socket === activeSocket
          ) {
            if (subscriptionRequestPending)
              replayRequiredForRun = requestedRunId
            else if (runId) beginRequest(activeSocket)
          }
        }
      }
    }

    function connect() {
      if (settled) return
      let activeSocket: WebSocket
      try {
        activeSocket = new WebSocket(runtimeUrl())
      } catch {
        scheduleReconnect()
        return
      }
      socket = activeSocket
      activeSocket.onopen = () => {
        if (socket === activeSocket && !settled) beginRequest(activeSocket)
      }
      activeSocket.onmessage = (message) =>
        handleMessage(activeSocket, message.data)
      activeSocket.onerror = () => activeSocket.close()
      activeSocket.onclose = () => {
        if (socket !== activeSocket || settled) return
        socket = null
        subscriptionRequestPending = false
        subscriptionReady = false
        replayRequiredForRun = null
        const closed = new RuntimeError(
          "connection_lost",
          "Connection to Trellis was lost."
        )
        for (const request of pending.values()) request.reject(closed)
        pending.clear()
        scheduleReconnect()
      }
    }

    connect()
  })
}

export function streamRun(
  input: StartRunInput,
  options: RuntimeClientOptions = {}
): Promise<RunResult> {
  return followRun(input, null, options, 0)
}

export function resumeRun(
  runId: string,
  options: RuntimeClientOptions = {},
  afterSequence = 0
): Promise<RunResult> {
  if (!runId || !Number.isSafeInteger(afterSequence) || afterSequence < 0)
    return Promise.reject(
      new RuntimeError("runtime_protocol_error", "Run cursor is invalid.")
    )
  return followRun(null, runId, options, afterSequence)
}

function requestRunAction(
  method: string,
  params: Record<string, unknown>,
  requestId: string,
  failureMessage: string
): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(runtimeUrl())
    let settled = false
    const finish = (result?: Record<string, unknown>, error?: RuntimeError) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      socket.close()
      if (error) reject(error)
      else if (result) resolve(result)
      else
        reject(
          new RuntimeError(
            "runtime_protocol_error",
            "The local service returned an invalid run."
          )
        )
    }
    const timeout = setTimeout(() => {
      finish(
        undefined,
        new RuntimeError(
          "runtime_timeout",
          "The local service did not respond."
        )
      )
    }, rpcTimeoutMs)
    socket.onopen = () => {
      try {
        socket.send(
          JSON.stringify({ jsonrpc: "2.0", id: requestId, method, params })
        )
      } catch {
        finish(
          undefined,
          new RuntimeError("connection_lost", "Connection to Trellis was lost.")
        )
      }
    }
    socket.onmessage = ({ data }) => {
      if (typeof data !== "string") return
      let response: unknown
      try {
        response = JSON.parse(data) as unknown
      } catch {
        finish(
          undefined,
          new RuntimeError(
            "runtime_protocol_error",
            "The local service sent invalid data."
          )
        )
        return
      }
      const envelope = asRecord(response)
      if (!envelope || envelope.id !== requestId) return
      if (envelope.error) {
        const error = asRecord(envelope.error)
        const detail = asRecord(error?.data)
        finish(
          undefined,
          new RuntimeError(
            typeof detail?.code === "string"
              ? detail.code
              : "runtime_request_failed",
            typeof error?.message === "string" ? error.message : failureMessage
          )
        )
        return
      }
      finish(asRecord(envelope.result) ?? undefined)
    }
    socket.onerror = () => {
      finish(
        undefined,
        new RuntimeError("connection_lost", "Connection to Trellis was lost.")
      )
    }
  })
}

export async function cancelRun(
  runId: string,
  afterSequence = 0
): Promise<{ runId: string; status: string }> {
  const result = await requestRunAction(
    "run.cancel",
    { runId, afterSequence },
    "runtime-cancel",
    "Trellis could not cancel that run."
  )
  if (typeof result.runId !== "string" || typeof result.status !== "string") {
    throw new RuntimeError(
      "runtime_protocol_error",
      "The local service returned an invalid run."
    )
  }
  return { runId: result.runId, status: result.status }
}

export async function respondToToolApproval(
  runId: string,
  toolCallId: string,
  decision: ToolApprovalDecision,
  afterSequence = 0
): Promise<{
  runId: string
  toolCallId: string
  decision: ToolApprovalDecision
}> {
  const result = await requestRunAction(
    "run.respond",
    { runId, toolCallId, decision, afterSequence },
    "runtime-respond",
    "Trellis could not answer that approval request."
  )
  if (
    result.runId !== runId ||
    result.toolCallId !== toolCallId ||
    result.decision !== decision
  ) {
    throw new RuntimeError(
      "runtime_protocol_error",
      "The local service returned a different approval."
    )
  }
  return { runId, toolCallId, decision }
}
