import type { RunSummary } from "@/lib/app-types"
import {
  AuthAccessError,
  getAuthController,
  type AuthController,
  type AuthCredential,
} from "@/lib/auth-controller"
import { runtimeUrl } from "@/lib/service-targets"
import {
  ServiceReadinessError,
  waitUntilServiceReady,
} from "@/lib/service-readiness"

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
  modelId?: string
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

export class RuntimeError extends Error {
  readonly code: string
  readonly status?: number

  constructor(code: string, message: string, status?: number) {
    super(message)
    this.code = code
    this.status = status
    this.name = "RuntimeError"
  }
}

type RpcResponse = {
  jsonrpc: "2.0"
  id: string | null
  result?: Record<string, unknown>
  error?: {
    code: number
    message: string
    data?: { code?: string; status?: number }
  }
}

type RpcRequest = {
  jsonrpc: "2.0"
  id: string
  method: string
  params: Record<string, unknown>
}

type RuntimeClientOptions = {
  signal?: AbortSignal
  onEvent?: (event: RuntimeRunEvent) => void
  onRunId?: (runId: string) => void
  onRunInfo?: (run: RuntimeRunInfo) => void
  reconnectAttempts?: number
  reconnectDelayMs?: number
}

type RunResult = { runId: string }

const rpcTimeoutMs = 30_000
const maxBufferedEvents = 500

function serviceStartupError(error: unknown): RuntimeError {
  const failure =
    error instanceof ServiceReadinessError ? error : new ServiceReadinessError()
  return new RuntimeError(failure.code, failure.message, 503)
}

function authenticationError() {
  return new RuntimeError(
    "authentication_required",
    "Sign in to continue.",
    401
  )
}

function expiredAuthenticationError() {
  return new RuntimeError(
    "reauth_required",
    "Your session expired. Please sign in again.",
    401
  )
}

function unavailableAuthenticationError() {
  return new RuntimeError(
    "auth_unavailable",
    "Sign-in verification is temporarily unavailable. Please try again.",
    503
  )
}

function credentialError(error: unknown): RuntimeError {
  if (error instanceof AuthAccessError) {
    return error.code === "auth_unavailable"
      ? unavailableAuthenticationError()
      : expiredAuthenticationError()
  }
  return authenticationError()
}

function expireRejectedCredential(
  auth: AuthController,
  credential: AuthCredential
) {
  if (!auth.isCredentialCurrent(credential)) return
  void (async () => {
    try {
      const current = await auth.getAccessToken()
      if (
        auth.isCredentialCurrent(credential) &&
        auth.isCredentialCurrent(current) &&
        current.token === credential.token
      ) {
        await auth.expireSession(credential)
      }
    } catch {
      // A newer or temporarily unavailable credential must remain untouched.
    }
  })()
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

function rpcError(
  response: RpcResponse,
  failureMessage = "Trellis could not start that run."
): RuntimeError {
  const error = asRecord(response.error)
  const detail = asRecord(error?.data)
  const code = detail?.code
  const status = typeof detail?.status === "number" ? detail.status : undefined
  if (code === "auth_failed" || code === "auth_required")
    return expiredAuthenticationError()
  if (code === "auth_unavailable") return unavailableAuthenticationError()
  return new RuntimeError(
    typeof code === "string" ? code : "runtime_request_failed",
    typeof error?.message === "string" ? error.message : failureMessage,
    status
  )
}

function credentialWasRejected(error: RuntimeError): boolean {
  return (
    error.status === 401 &&
    (error.code === "reauth_required" || error.code === "cloud_access_denied")
  )
}

function parseRunInfo(result: Record<string, unknown>): RuntimeRunInfo {
  const runId = result.runId as string
  const info: RuntimeRunInfo = { runId }
  if (typeof result.modelId === "string") info.modelId = result.modelId
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
  const auth = getAuthController()
  const initialUser = auth.getSnapshot().user?.id
  const initialEpoch = auth.getSessionEpoch()

  return new Promise((resolve, reject) => {
    const startup = new AbortController()
    const abort = () =>
      finish(new RuntimeError("connection_cancelled", "Connection cancelled."))
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
    let openingTimer: ReturnType<typeof setTimeout> | null = null
    let socketCredential: AuthCredential | null = null
    let unsubscribeAuth: (() => void) | null = null
    const pending = new Map<
      string,
      { resolve: (value: RpcResponse) => void; reject: (reason: Error) => void }
    >()
    const bufferedEvents: RuntimeRunEvent[] = []

    const stillCurrent = () => {
      const current = auth.getSnapshot()
      return (
        current.status === "signed-in" &&
        current.user?.id === initialUser &&
        auth.getSessionEpoch() === initialEpoch
      )
    }

    const finish = (error?: Error) => {
      if (settled) return
      settled = true
      startup.abort()
      options.signal?.removeEventListener("abort", abort)
      unsubscribeAuth?.()
      unsubscribeAuth = null
      if (reconnectTimer !== null) clearTimeout(reconnectTimer)
      if (openingTimer !== null) clearTimeout(openingTimer)
      openingTimer = null
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

    const failRequest = (error: RuntimeError) => {
      const rejectedCredential = socketCredential
      finish(error)
      if (credentialWasRejected(error) && rejectedCredential)
        expireRejectedCredential(auth, rejectedCredential)
    }

    const processEvent = (event: RuntimeRunEvent) => {
      if (settled || !runId || event.runId !== runId) return
      if (!stillCurrent()) {
        finish(authenticationError())
        return
      }
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
      if (settled || socket !== activeSocket || !stillCurrent())
        return Promise.reject(authenticationError())
      const id = `runtime-${++nextRpcId}`
      const request: RpcRequest = { jsonrpc: "2.0", id, method, params }
      return new Promise<RpcResponse>((resolveRpc, rejectRpc) => {
        const timer = setTimeout(() => {
          pending.delete(id)
          rejectRpc(
            new RuntimeError(
              "runtime_timeout",
              "The Trellis service did not respond.",
              503
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
      if (settled || socket !== activeSocket) return
      if (!stillCurrent()) {
        finish(authenticationError())
        return
      }
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
          if (!stillCurrent()) throw authenticationError()
          subscriptionRequestPending = false
          if (response.error) throw rpcError(response)
          const result = response.result
          const responseRunId = result?.runId
          if (!result || typeof responseRunId !== "string") {
            throw new RuntimeError(
              "runtime_protocol_error",
              "The Trellis service returned an invalid run."
            )
          }
          if (runId && responseRunId !== runId) {
            throw new RuntimeError(
              "runtime_protocol_error",
              "The Trellis service resumed a different run."
            )
          }
          const runInfo = parseRunInfo(result)
          if (!runId) runId = responseRunId
          if (!runIdNotified) {
            runIdNotified = true
            options.onRunId?.(runId)
          }
          if (settled || !stillCurrent()) return
          options.onRunInfo?.(runInfo)
          if (settled || !stillCurrent()) return
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
          failRequest(runtimeError)
        })
    }

    const scheduleReconnect = () => {
      if (settled || reconnectTimer !== null) return
      if (!stillCurrent()) {
        finish(authenticationError())
        return
      }
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
        void connect()
      }, reconnectDelay * reconnects)
    }

    const handleMessage = (activeSocket: WebSocket, raw: unknown) => {
      if (settled || socket !== activeSocket) return
      if (!stillCurrent()) {
        finish(authenticationError())
        return
      }
      if (typeof raw !== "string") return
      let decoded: unknown
      try {
        decoded = JSON.parse(raw) as unknown
      } catch {
        finish(
          new RuntimeError(
            "runtime_protocol_error",
            "The Trellis service sent invalid data."
          )
        )
        return
      }
      const messages = Array.isArray(decoded) ? decoded : [decoded]
      for (const value of messages) {
        if (settled || !stillCurrent()) return
        const message = asRecord(value)
        if (!message || message.jsonrpc !== "2.0") continue
        if (message.id === null && message.error) {
          const error = rpcError(message as unknown as RpcResponse)
          failRequest(error)
          return
        }
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

    async function connect() {
      if (settled) return
      if (!stillCurrent()) {
        finish(authenticationError())
        return
      }
      try {
        await waitUntilServiceReady(startup.signal)
      } catch (error) {
        if (!settled)
          finish(
            stillCurrent() ? serviceStartupError(error) : authenticationError()
          )
        return
      }
      if (settled) return
      if (!stillCurrent()) {
        finish(authenticationError())
        return
      }
      let credential: AuthCredential
      try {
        credential = await auth.getAccessToken()
      } catch (error) {
        finish(credentialError(error))
        return
      }
      if (settled) return
      if (!stillCurrent() || !auth.isCredentialCurrent(credential)) {
        finish(authenticationError())
        return
      }
      let activeSocket: WebSocket
      try {
        activeSocket = new WebSocket(runtimeUrl())
      } catch {
        scheduleReconnect()
        return
      }
      socket = activeSocket
      socketCredential = null
      activeSocket.onopen = () => {
        if (socket !== activeSocket || settled) return
        if (openingTimer !== null) clearTimeout(openingTimer)
        openingTimer = null
        void (async () => {
          try {
            const freshCredential = await auth.getAccessToken()
            if (socket !== activeSocket || settled) return
            if (
              freshCredential.userId !== credential.userId ||
              freshCredential.sessionEpoch !== credential.sessionEpoch ||
              !auth.isCredentialCurrent(freshCredential) ||
              !stillCurrent()
            ) {
              throw authenticationError()
            }
            socketCredential = freshCredential
            const response = await sendRpc(activeSocket, "auth.authenticate", {
              accessToken: freshCredential.token,
            })
            if (socket !== activeSocket || settled) return
            if (
              response.error ||
              response.result?.userId !== credential.userId ||
              !stillCurrent()
            ) {
              if (response.error) throw rpcError(response)
              throw authenticationError()
            }
            beginRequest(activeSocket)
          } catch (error) {
            if (socket === activeSocket && !settled)
              failRequest(
                error instanceof RuntimeError ? error : credentialError(error)
              )
          }
        })()
      }
      activeSocket.onmessage = (message) =>
        handleMessage(activeSocket, message.data)
      activeSocket.onerror = () => activeSocket.close()
      activeSocket.onclose = ({ code }) => {
        if (socket !== activeSocket || settled) return
        if (openingTimer !== null) clearTimeout(openingTimer)
        openingTimer = null
        if (!stillCurrent()) {
          finish(authenticationError())
          return
        }
        if (code === 4401 || code === 1013) {
          failRequest(
            code === 4401
              ? expiredAuthenticationError()
              : unavailableAuthenticationError()
          )
          return
        }
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
      openingTimer = setTimeout(() => {
        if (socket !== activeSocket || settled) return
        finish(
          stillCurrent()
            ? new RuntimeError(
                "runtime_timeout",
                "Connection to Trellis timed out. Please try again.",
                503
              )
            : authenticationError()
        )
      }, rpcTimeoutMs)
    }

    unsubscribeAuth = auth.subscribe(() => {
      if (!stillCurrent()) finish(authenticationError())
    })
    options.signal?.addEventListener("abort", abort, { once: true })
    if (options.signal?.aborted) abort()
    void connect()
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

async function requestRunAction<TResult>(
  method: "run.cancel" | "run.respond",
  params: Record<string, unknown>,
  requestId: string,
  failureMessage: string,
  parseResult: (result: Record<string, unknown>) => TResult
): Promise<TResult> {
  const auth = getAuthController()
  const initialUser = auth.getSnapshot().user?.id
  const initialEpoch = auth.getSessionEpoch()
  const stillCurrent = () => {
    const current = auth.getSnapshot()
    return (
      current.status === "signed-in" &&
      current.user?.id === initialUser &&
      auth.getSessionEpoch() === initialEpoch
    )
  }
  if (!stillCurrent()) throw authenticationError()
  const startup = new AbortController()
  const unsubscribeStartup = auth.subscribe(() => {
    if (!stillCurrent()) startup.abort()
  })
  try {
    await waitUntilServiceReady(startup.signal)
  } catch (error) {
    if (!stillCurrent()) throw authenticationError()
    throw serviceStartupError(error)
  } finally {
    unsubscribeStartup()
    startup.abort()
  }
  if (!stillCurrent()) throw authenticationError()
  let credential: AuthCredential
  try {
    credential = await auth.getAccessToken()
  } catch (error) {
    throw credentialError(error)
  }
  if (!stillCurrent() || !auth.isCredentialCurrent(credential))
    throw authenticationError()
  let socket: WebSocket
  try {
    socket = new WebSocket(runtimeUrl())
  } catch {
    throw new RuntimeError("connection_lost", "Connection to Trellis was lost.")
  }
  return new Promise((resolve, reject) => {
    let settled = false
    let authenticated = false
    let socketCredential: AuthCredential | null = null
    let unsubscribeAuth: (() => void) | null = null
    const finish = (result?: TResult, error?: RuntimeError) => {
      if (settled) return
      settled = true
      unsubscribeAuth?.()
      unsubscribeAuth = null
      clearTimeout(timeout)
      socket.close()
      if (error) {
        reject(error)
        if (credentialWasRejected(error) && socketCredential)
          expireRejectedCredential(auth, socketCredential)
      } else if (result) resolve(result)
      else
        reject(
          new RuntimeError(
            "runtime_protocol_error",
            "The Trellis service returned an invalid run."
          )
        )
    }
    const timeout = setTimeout(() => {
      finish(
        undefined,
        new RuntimeError(
          "runtime_timeout",
          "The Trellis service did not respond.",
          503
        )
      )
    }, rpcTimeoutMs)
    socket.onopen = () => {
      if (settled) return
      void (async () => {
        let freshCredential: AuthCredential
        try {
          freshCredential = await auth.getAccessToken()
        } catch (error) {
          finish(undefined, credentialError(error))
          return
        }
        if (settled) return
        if (
          freshCredential.userId !== credential.userId ||
          freshCredential.sessionEpoch !== credential.sessionEpoch ||
          !auth.isCredentialCurrent(freshCredential) ||
          !stillCurrent()
        ) {
          finish(undefined, authenticationError())
          return
        }
        socketCredential = freshCredential
        try {
          socket.send(
            JSON.stringify({
              jsonrpc: "2.0",
              id: "runtime-auth",
              method: "auth.authenticate",
              params: { accessToken: freshCredential.token },
            })
          )
        } catch {
          finish(
            undefined,
            new RuntimeError(
              "connection_lost",
              "Connection to Trellis was lost."
            )
          )
        }
      })()
    }
    socket.onmessage = ({ data }) => {
      if (settled) return
      if (!stillCurrent()) {
        finish(undefined, authenticationError())
        return
      }
      if (typeof data !== "string") return
      let response: unknown
      try {
        response = JSON.parse(data) as unknown
      } catch {
        finish(
          undefined,
          new RuntimeError(
            "runtime_protocol_error",
            "The Trellis service sent invalid data."
          )
        )
        return
      }
      const envelope = asRecord(response)
      if (!envelope) return
      if (envelope.id === null && envelope.error) {
        finish(undefined, rpcError(envelope as unknown as RpcResponse))
        return
      }
      if (envelope.id === "runtime-auth") {
        if (authenticated) return
        if (envelope.error) {
          finish(undefined, rpcError(envelope as unknown as RpcResponse))
          return
        }
        if (
          asRecord(envelope.result)?.userId !== credential.userId ||
          !stillCurrent()
        ) {
          finish(undefined, authenticationError())
          return
        }
        authenticated = true
        try {
          socket.send(
            JSON.stringify({ jsonrpc: "2.0", id: requestId, method, params })
          )
        } catch {
          finish(
            undefined,
            new RuntimeError(
              "connection_lost",
              "Connection to Trellis was lost."
            )
          )
        }
        return
      }
      if (!authenticated || envelope.id !== requestId) return
      if (!stillCurrent()) {
        finish(undefined, authenticationError())
        return
      }
      if (envelope.error) {
        finish(
          undefined,
          rpcError(envelope as unknown as RpcResponse, failureMessage)
        )
        return
      }
      const result = asRecord(envelope.result)
      if (!result) {
        finish()
        return
      }
      try {
        finish(parseResult(result))
      } catch (error) {
        finish(
          undefined,
          error instanceof RuntimeError
            ? error
            : new RuntimeError(
                "runtime_protocol_error",
                "The Trellis service returned an invalid run."
              )
        )
      }
    }
    socket.onerror = () => {
      finish(
        undefined,
        new RuntimeError("connection_lost", "Connection to Trellis was lost.")
      )
    }
    socket.onclose = ({ code }) => {
      finish(
        undefined,
        !stillCurrent()
          ? authenticationError()
          : code === 4401
            ? expiredAuthenticationError()
            : code === 1013
              ? unavailableAuthenticationError()
              : new RuntimeError(
                  "connection_lost",
                  "Connection to Trellis was lost."
                )
      )
    }
    unsubscribeAuth = auth.subscribe(() => {
      if (!stillCurrent()) finish(undefined, authenticationError())
    })
    if (!stillCurrent()) finish(undefined, authenticationError())
  })
}

export async function cancelRun(
  runId: string,
  afterSequence = 0
): Promise<{ runId: string; status: string }> {
  return requestRunAction(
    "run.cancel",
    { runId, afterSequence },
    "runtime-cancel",
    "Trellis could not cancel that run.",
    (result) => {
      if (result.runId !== runId || typeof result.status !== "string") {
        throw new RuntimeError(
          "runtime_protocol_error",
          "The Trellis service returned an invalid run."
        )
      }
      return { runId: result.runId, status: result.status }
    }
  )
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
  return requestRunAction(
    "run.respond",
    { runId, toolCallId, decision, afterSequence },
    "runtime-respond",
    "Trellis could not answer that approval request.",
    (result) => {
      if (
        result.runId !== runId ||
        result.toolCallId !== toolCallId ||
        result.decision !== decision
      ) {
        throw new RuntimeError(
          "runtime_protocol_error",
          "The Trellis service returned a different approval."
        )
      }
      return { runId, toolCallId, decision }
    }
  )
}
