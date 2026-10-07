import type {
  Profile,
  LatestRun,
  RunSummary,
  SavedRunEvent,
  OnboardingState,
  ProviderId,
  Session,
  SessionDetail,
  Settings,
  TestPreset,
} from "@/lib/app-types"

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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  })

  if (!response.ok) {
    let payload: ErrorPayload = {}
    try {
      payload = (await response.json()) as ErrorPayload
    } catch {
      // Responses from upstreams are deliberately not exposed to the UI.
    }
    throw new ApiError(
      payload.error?.code ?? "request_failed",
      payload.error?.message ??
        payload.detail ??
        "Trellis could not complete that request.",
      response.status
    )
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  getProfile: () => request<Profile>("/api/profile"),
  updateProfile: (profile: Pick<Profile, "display_name" | "email">) =>
    request<Profile>("/api/profile", {
      method: "PUT",
      body: JSON.stringify(profile),
    }),
  getSettings: () => request<Settings>("/api/settings"),
  getOnboarding: () => request<OnboardingState>("/api/onboarding"),
  completeOnboardingIntro: () =>
    request<OnboardingState>("/api/onboarding/steps/intro", { method: "PUT" }),
  saveOnboardingProfile: (profile: Pick<Profile, "display_name" | "email">) =>
    request<OnboardingState>("/api/onboarding/steps/profile", {
      method: "PUT",
      body: JSON.stringify(profile),
    }),
  saveOnboardingModel: (modelId: string, apiKey: string) =>
    request<OnboardingState>("/api/onboarding/steps/model", {
      method: "PUT",
      body: JSON.stringify({ model_id: modelId, api_key: apiKey || null }),
    }),
  selectProvider: (provider: ProviderId) =>
    request<Settings>("/api/settings/provider", {
      method: "PUT",
      body: JSON.stringify({ provider }),
    }),
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
  saveApiKey: (provider: ProviderId, apiKey: string) =>
    request<Settings>(`/api/settings/providers/${provider}/api-key`, {
      method: "PUT",
      body: JSON.stringify({ api_key: apiKey }),
    }),
  removeApiKey: (provider: ProviderId) =>
    request<Settings>(`/api/settings/providers/${provider}/api-key`, {
      method: "DELETE",
    }),
  listSessions: () => request<Session[]>("/api/sessions"),
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
  listTestPresets: (sessionId: string) =>
    request<TestPreset[]>(`/api/sessions/${sessionId}/test-presets`),
  saveTestPreset: (sessionId: string, preset: TestPreset) =>
    request<TestPreset>(`/api/sessions/${sessionId}/test-presets`, {
      method: "POST",
      body: JSON.stringify(preset),
    }),
  deleteTestPreset: (sessionId: string, name: string) =>
    request<void>(
      `/api/sessions/${sessionId}/test-presets/${encodeURIComponent(name)}`,
      { method: "DELETE" }
    ),
}
