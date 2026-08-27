import type {
  Profile,
  ProviderId,
  Session,
  SessionDetail,
  Settings,
  TurnResult,
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
  selectProvider: (provider: ProviderId) =>
    request<Settings>("/api/settings/provider", {
      method: "PUT",
      body: JSON.stringify({ provider }),
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
  createSession: () => request<Session>("/api/sessions", { method: "POST" }),
  getSession: (sessionId: string) =>
    request<SessionDetail>(`/api/sessions/${sessionId}`),
  completeTurn: (sessionId: string, turnId: string, content: string) =>
    request<TurnResult>(`/api/sessions/${sessionId}/turns`, {
      method: "POST",
      body: JSON.stringify({ turn_id: turnId, content }),
    }),
}
