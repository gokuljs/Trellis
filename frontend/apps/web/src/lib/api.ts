import type {
  Profile,
  LatestRun,
  RunSummary,
  SavedRunEvent,
  OnboardingState,
  Session,
  SessionDetail,
  Settings,
} from "@/lib/app-types"
import { getAuthController } from "@/lib/auth-controller"

type ErrorPayload = {
  error?: {
    code?: string
    message?: string
  }
  detail?: string
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

const onboardingRequestTimeout = 15000

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: RequestOptions = {}
): Promise<T> {
  if (!options.signal && options.timeoutMs === undefined)
    return authorizedRequest<T>(path, init)

  const controller = new AbortController()
  const abort = () => controller.abort(options.signal?.reason)
  let timer: ReturnType<typeof setTimeout> | undefined
  const aborted = new Promise<never>((_, reject) => {
    controller.signal.addEventListener(
      "abort",
      () => reject(controller.signal.reason),
      { once: true }
    )
  })
  options.signal?.addEventListener("abort", abort, { once: true })
  if (options.signal?.aborted) abort()
  if (options.timeoutMs !== undefined) {
    timer = setTimeout(
      () =>
        controller.abort(
          new ApiError(
            "request_timeout",
            "Trellis took too long to respond. Try again.",
            0
          )
        ),
      options.timeoutMs
    )
  }
  try {
    return await Promise.race([
      authorizedRequest<T>(path, { ...init, signal: controller.signal }),
      aborted,
    ])
  } finally {
    clearTimeout(timer)
    options.signal?.removeEventListener("abort", abort)
  }
}

async function authorizedRequest<T>(
  path: string,
  init: RequestInit
): Promise<T> {
  init.signal?.throwIfAborted()
  const auth = getAuthController()
  const { token, userId } = await auth.getAccessToken()
  init.signal?.throwIfAborted()
  const sessionEpoch = auth.getSessionEpoch()
  const headers = new Headers(init.headers)
  headers.set("Accept", "application/json")
  if (init.body) headers.set("Content-Type", "application/json")
  headers.set("Authorization", `Bearer ${token}`)
  const response = await fetch(path, {
    ...init,
    headers,
  })
  init.signal?.throwIfAborted()

  function assertCurrentUser() {
    init.signal?.throwIfAborted()
    const current = auth.getSnapshot()
    if (
      current.status !== "signed-in" ||
      current.user?.id !== userId ||
      auth.getSessionEpoch() !== sessionEpoch
    ) {
      throw new ApiError(
        "authentication_required",
        "Sign in again to continue.",
        401
      )
    }
  }

  assertCurrentUser()
  if (response.status === 401) {
    let stillRejectedToken = false
    try {
      const currentCredential = await auth.getAccessToken()
      assertCurrentUser()
      stillRejectedToken =
        currentCredential.userId === userId && currentCredential.token === token
    } catch {
      // An old request must not sign out a newer or unavailable session.
    }
    if (stillRejectedToken) void auth.signOut()
    throw new ApiError(
      "authentication_required",
      "Sign in again to continue.",
      401
    )
  }

  if (!response.ok) {
    let payload: ErrorPayload = {}
    try {
      payload = (await response.json()) as ErrorPayload
    } catch {
      // Responses from upstreams are deliberately not exposed to the UI.
    }
    assertCurrentUser()
    throw new ApiError(
      payload.error?.code ?? "request_failed",
      payload.error?.message ??
        payload.detail ??
        "Trellis could not complete that request.",
      response.status
    )
  }

  if (response.status === 204) return undefined as T
  const payload = (await response.json()) as T
  init.signal?.throwIfAborted()
  assertCurrentUser()
  return payload
}

export const api = {
  getProfile: (options: RequestOptions = {}) =>
    request<Profile>(
      "/api/profile",
      {},
      { timeoutMs: onboardingRequestTimeout, ...options }
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
      { timeoutMs: onboardingRequestTimeout, ...options }
    ),
  getOnboarding: (options: RequestOptions = {}) =>
    request<OnboardingState>(
      "/api/onboarding",
      {},
      { timeoutMs: onboardingRequestTimeout, ...options }
    ),
  completeOnboardingIntro: (options: RequestOptions = {}) =>
    request<OnboardingState>(
      "/api/onboarding/steps/intro",
      { method: "PUT" },
      { timeoutMs: onboardingRequestTimeout, ...options }
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
      { timeoutMs: onboardingRequestTimeout, ...options }
    ),
  saveOnboardingModel: (modelId: string, options: RequestOptions = {}) =>
    request<OnboardingState>(
      "/api/onboarding/steps/model",
      {
        method: "PUT",
        body: JSON.stringify({ model_id: modelId }),
      },
      { timeoutMs: onboardingRequestTimeout, ...options }
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
  pickWorkspace: () =>
    request<{ path: string | null }>("/api/workspaces/pick", {
      method: "POST",
    }),
  createSession: (workspacePath?: string) =>
    request<Session>("/api/sessions", {
      method: "POST",
      ...(workspacePath
        ? { body: JSON.stringify({ workspace_path: workspacePath }) }
        : {}),
    }),
  setSessionWorkspace: (sessionId: string, workspacePath: string | null) =>
    request<Session>(`/api/sessions/${sessionId}/workspace`, {
      method: "PUT",
      body: JSON.stringify({ workspace_path: workspacePath }),
    }),
  getSession: (sessionId: string) =>
    request<SessionDetail>(`/api/sessions/${sessionId}`),
  getLatestRun: (sessionId: string) =>
    request<LatestRun | null>(`/api/sessions/${sessionId}/runs/latest`),
  listRunSummaries: (sessionId: string, offset: number) =>
    request<{ items: RunSummary[]; next_offset: number | null }>(
      `/api/sessions/${sessionId}/runs?offset=${offset}&limit=100`
    ),
  listRunEvents: (sessionId: string, runId: string, afterSequence: number) =>
    request<{ items: SavedRunEvent[]; next_after_sequence: number | null }>(
      `/api/sessions/${sessionId}/runs/${encodeURIComponent(runId)}/events?after_sequence=${afterSequence}&limit=500`
    ),
}
