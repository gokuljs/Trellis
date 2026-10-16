import type {
  Profile,
  LatestRun,
  RunSummary,
  SavedRunEvent,
  OnboardingState,
  Session,
  SessionDetail,
  SessionHistoryPage,
  SessionPage,
  Settings,
  ContextPreview,
  ContextPreviewRequest,
} from "@/lib/app-types"
import {
  AuthAccessError,
  getAuthController,
  type AuthController,
  type AuthCredential,
} from "@/lib/auth-controller"
import { configuredApiUrl } from "@/lib/service-targets"
import { waitUntilServiceReady } from "@/lib/service-readiness"

type ErrorPayload = {
  error?: {
    code?: string
    message?: string
  }
  detail?: string
}

function readErrorPayload(value: unknown): ErrorPayload {
  if (typeof value !== "object" || value === null) return {}
  const { error, detail } = value as Record<string, unknown>
  const fields =
    typeof error === "object" && error !== null
      ? (error as Record<string, unknown>)
      : {}
  return {
    error: {
      code: typeof fields.code === "string" ? fields.code : undefined,
      message: typeof fields.message === "string" ? fields.message : undefined,
    },
    detail: typeof detail === "string" ? detail : undefined,
  }
}

export class ApiError extends Error {
  code: string
  status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.code = code
    this.status = status
  }
}

type RequestOptions = {
  signal?: AbortSignal
  timeoutMs?: number
}

const requestTimeoutMs = 15000

type RequestAuth = Pick<
  AuthController,
  "getSnapshot" | "getAccessToken" | "isCredentialCurrent" | "expireSession"
>

type ApiClientDependencies = {
  getAuthController?: () => RequestAuth
  fetch?: typeof globalThis.fetch
  waitUntilReady?: (signal: AbortSignal) => Promise<void>
  resolveUrl?: (path: string) => string
}

function authenticationRequired() {
  return new ApiError(
    "authentication_required",
    "Sign in again to continue.",
    401
  )
}

function serviceUnavailable() {
  return new ApiError(
    "service_unavailable",
    "Trellis could not reach the service. Check your connection and try again.",
    0
  )
}

export function createApiClient({
  getAuthController: resolveAuth = getAuthController,
  fetch: fetchRequest = (path, init) => globalThis.fetch(path, init),
  waitUntilReady = waitUntilServiceReady,
  resolveUrl = configuredApiUrl,
}: ApiClientDependencies = {}) {
  async function request<T>(
    path: string,
    init: RequestInit = {},
    options: RequestOptions = {}
  ): Promise<T> {
    let auth: RequestAuth
    try {
      auth = resolveAuth()
    } catch {
      throw serviceUnavailable()
    }
    const original = auth.getSnapshot()
    const sessionEpoch = original.sessionEpoch
    const userId = original.status === "signed-in" ? original.user?.id : null
    if (!userId) throw authenticationRequired()
    const controller = new AbortController()
    const abort = () => controller.abort(options.signal?.reason)
    const aborted = new Promise<never>((_, reject) => {
      controller.signal.addEventListener(
        "abort",
        () => reject(controller.signal.reason),
        { once: true }
      )
    })
    options.signal?.addEventListener("abort", abort, { once: true })
    if (options.signal?.aborted) abort()
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      return await Promise.race([
        (async () => {
          await waitUntilReady(controller.signal)
          controller.signal.throwIfAborted()
          timer = setTimeout(
            () =>
              controller.abort(
                new ApiError(
                  "request_timeout",
                  "Trellis took too long to respond. Try again.",
                  0
                )
              ),
            options.timeoutMs ?? requestTimeoutMs
          )
          return authorizedRequest<T>(
            path,
            {
              ...init,
              signal: controller.signal,
            },
            auth,
            userId,
            sessionEpoch
          )
        })(),
        aborted,
      ])
    } finally {
      clearTimeout(timer)
      options.signal?.removeEventListener("abort", abort)
    }
  }

  async function authorizedRequest<T>(
    path: string,
    init: RequestInit,
    auth: RequestAuth,
    userId: string,
    sessionEpoch: number
  ): Promise<T> {
    init.signal?.throwIfAborted()

    function assertOriginalAccount() {
      init.signal?.throwIfAborted()
      const current = auth.getSnapshot()
      if (
        current.status !== "signed-in" ||
        current.user?.id !== userId ||
        current.sessionEpoch !== sessionEpoch
      )
        throw authenticationRequired()
    }

    assertOriginalAccount()
    let credential: AuthCredential
    try {
      credential = await auth.getAccessToken()
    } catch (error) {
      assertOriginalAccount()
      if (error instanceof AuthAccessError && error.code === "reauth_required")
        throw authenticationRequired()
      throw serviceUnavailable()
    }

    function assertCurrentCredential() {
      assertOriginalAccount()
      if (
        credential.userId !== userId ||
        credential.sessionEpoch !== sessionEpoch ||
        !auth.isCredentialCurrent(credential)
      )
        throw authenticationRequired()
    }

    assertCurrentCredential()
    const headers = new Headers(init.headers)
    headers.set("Accept", "application/json")
    if (init.body) headers.set("Content-Type", "application/json")
    headers.set("Authorization", `Bearer ${credential.token}`)
    let response: Response
    try {
      response = await fetchRequest(resolveUrl(path), {
        ...init,
        headers,
      })
    } catch {
      assertCurrentCredential()
      throw serviceUnavailable()
    }

    assertCurrentCredential()
    if (response.status === 401) {
      let currentCredential: AuthCredential
      try {
        currentCredential = await auth.getAccessToken()
      } catch (error) {
        assertCurrentCredential()
        if (
          error instanceof AuthAccessError &&
          error.code === "reauth_required"
        )
          throw authenticationRequired()
        throw serviceUnavailable()
      }
      assertCurrentCredential()
      if (
        currentCredential.userId === credential.userId &&
        currentCredential.sessionEpoch === credential.sessionEpoch &&
        currentCredential.token === credential.token
      )
        void auth.expireSession(credential).catch(() => {
          // Expiry locks the account synchronously; cleanup cannot restore it.
        })
      throw authenticationRequired()
    }

    if (!response.ok) {
      let payload: ErrorPayload = {}
      try {
        payload = readErrorPayload(await response.json())
      } catch {
        // Responses from upstreams are deliberately not exposed to the UI.
      }
      assertCurrentCredential()
      throw new ApiError(
        payload.error?.code ?? "request_failed",
        payload.error?.message ??
          payload.detail ??
          "Trellis could not complete that request.",
        response.status
      )
    }

    if (response.status === 204) return undefined as T
    let payload: T
    try {
      payload = (await response.json()) as T
    } catch {
      assertCurrentCredential()
      throw serviceUnavailable()
    }
    assertCurrentCredential()
    return payload
  }

  return {
    getProfile: (options: RequestOptions = {}) =>
      request<Profile>(
        "/api/profile",
        {},
        { timeoutMs: requestTimeoutMs, ...options }
      ),
    updateProfile: (profile: Pick<Profile, "display_name">) =>
      request<Profile>("/api/profile", {
        method: "PUT",
        body: JSON.stringify(profile),
      }),
    getSettings: (options: RequestOptions = {}) =>
      request<Settings>(
        "/api/settings",
        {},
        { timeoutMs: requestTimeoutMs, ...options }
      ),
    previewContext: (
      input: ContextPreviewRequest,
      options: RequestOptions = {}
    ) =>
      request<ContextPreview>(
        "/api/context-preview",
        {
          method: "POST",
          body: JSON.stringify(input),
        },
        options
      ),
    getOnboarding: (options: RequestOptions = {}) =>
      request<OnboardingState>(
        "/api/onboarding",
        {},
        { timeoutMs: requestTimeoutMs, ...options }
      ),
    completeOnboardingIntro: (options: RequestOptions = {}) =>
      request<OnboardingState>(
        "/api/onboarding/steps/intro",
        { method: "PUT" },
        { timeoutMs: requestTimeoutMs, ...options }
      ),
    saveOnboardingProfile: (
      profile: Pick<Profile, "display_name">,
      options: RequestOptions = {}
    ) =>
      request<OnboardingState>(
        "/api/onboarding/steps/profile",
        {
          method: "PUT",
          body: JSON.stringify(profile),
        },
        { timeoutMs: requestTimeoutMs, ...options }
      ),
    saveOnboardingModel: (modelId: string, options: RequestOptions = {}) =>
      request<OnboardingState>(
        "/api/onboarding/steps/model",
        {
          method: "PUT",
          body: JSON.stringify({ model_id: modelId }),
        },
        { timeoutMs: requestTimeoutMs, ...options }
      ),
    selectModel: (modelId: string) =>
      request<Settings>("/api/settings/model", {
        method: "PUT",
        body: JSON.stringify({ model_id: modelId }),
      }),
    selectDefaultBudget: (budgetPreset: "conservative" | "longer") =>
      request<Settings>("/api/settings/budget", {
        method: "PUT",
        body: JSON.stringify({ budget_preset: budgetPreset }),
      }),
    listSessions: (options: RequestOptions = {}) =>
      request<Session[]>("/api/sessions", {}, options),
    listSessionPages: (
      beforeCursor: string | null,
      options: RequestOptions = {}
    ) =>
      request<SessionPage>(
        `/api/session-pages?limit=40${beforeCursor === null ? "" : `&before_cursor=${encodeURIComponent(beforeCursor)}`}`,
        {},
        options
      ),
    getSessionHistory: (
      sessionId: string,
      beforeCursor: string | null,
      options: RequestOptions = {}
    ) =>
      request<SessionHistoryPage>(
        `/api/sessions/${encodeURIComponent(sessionId)}/history?limit=40${beforeCursor === null ? "" : `&before_cursor=${encodeURIComponent(beforeCursor)}`}`,
        {},
        options
      ),
    createSession: () =>
      request<Session>("/api/sessions", {
        method: "POST",
      }),
    getSession: (sessionId: string) =>
      request<SessionDetail>(`/api/sessions/${sessionId}`),
    selectSessionModel: (sessionId: string, modelId: string) =>
      request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}/model`, {
        method: "PUT",
        body: JSON.stringify({ model_id: modelId }),
      }),
    getLatestRun: (sessionId: string) =>
      request<LatestRun | null>(`/api/sessions/${sessionId}/runs/latest`),
    listRunSummaries: (
      sessionId: string,
      offset: number,
      options: RequestOptions = {},
      filters: { turnId?: string; newestFirst?: boolean } = {}
    ) =>
      request<{ items: RunSummary[]; next_offset: number | null }>(
        `/api/sessions/${encodeURIComponent(sessionId)}/runs?offset=${offset}&limit=100${filters.newestFirst ? "&newest_first=true" : ""}${filters.turnId ? `&turn_id=${encodeURIComponent(filters.turnId)}` : ""}`,
        {},
        options
      ),
    listRunEvents: (
      sessionId: string,
      runId: string,
      afterSequence: number,
      options: RequestOptions = {}
    ) =>
      request<{ items: SavedRunEvent[]; next_after_sequence: number | null }>(
        `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/events?after_sequence=${afterSequence}&limit=100`,
        {},
        options
      ),
  }
}

export const api = createApiClient()
