import { afterEach, beforeEach, expect, it, vi } from "vitest"

import { api } from "@/lib/api"

const auth = vi.hoisted(() => ({
  getAccessToken: vi.fn(),
  getSessionEpoch: vi.fn(),
  getSnapshot: vi.fn(),
  signOut: vi.fn(),
}))

vi.mock("@/lib/auth-controller", () => ({
  getAuthController: () => auth,
}))

beforeEach(() => {
  auth.getSessionEpoch.mockReturnValue(0)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

it("bounds onboarding reads even when the response body stalls", async () => {
  vi.useFakeTimers()
  auth.getAccessToken.mockResolvedValue({ token: "fresh-token", userId: "ada" })
  auth.getSnapshot.mockReturnValue({ status: "signed-in", user: { id: "ada" } })
  const stalled = new Response("", { status: 200 })
  vi.spyOn(stalled, "json").mockReturnValue(new Promise(() => {}))
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(stalled))

  let error: unknown
  void api.getOnboarding().catch((failure) => {
    error = failure
  })
  await vi.advanceTimersByTimeAsync(15000)
  expect(error).toMatchObject({ code: "request_timeout" })
})

it("aborts onboarding writes when their deadline expires", async () => {
  vi.useFakeTimers()
  auth.getAccessToken.mockResolvedValue({ token: "fresh-token", userId: "ada" })
  auth.getSnapshot.mockReturnValue({ status: "signed-in", user: { id: "ada" } })
  let signal: AbortSignal | undefined
  vi.stubGlobal(
    "fetch",
    vi.fn((_url, init: RequestInit) => {
      signal = init.signal ?? undefined
      return new Promise(() => {})
    })
  )
  let error: unknown
  void api.saveOnboardingProfile({ display_name: "Ada" }).catch((failure) => {
    error = failure
  })
  await vi.advanceTimersByTimeAsync(15000)
  expect(error).toMatchObject({ code: "request_timeout" })
  expect(signal?.aborted).toBe(true)
})

it("cancels a bootstrap read before a delayed credential can contact the backend", async () => {
  let resolveToken!: (value: { token: string; userId: string }) => void
  auth.getAccessToken.mockReturnValue(
    new Promise((resolve) => {
      resolveToken = resolve
    })
  )
  const fetch = vi.fn()
  vi.stubGlobal("fetch", fetch)
  const controller = new AbortController()
  let error: unknown
  void api.getProfile({ signal: controller.signal }).catch((failure) => {
    error = failure
  })
  controller.abort()
  await vi.waitFor(() => expect(error).toMatchObject({ name: "AbortError" }))
  resolveToken({ token: "old-token", userId: "ada" })
  await Promise.resolve()
  expect(fetch).not.toHaveBeenCalled()
})

it("sends the current access token on backend HTTP requests", async () => {
  auth.getAccessToken.mockResolvedValue({ token: "fresh-token", userId: "ada" })
  auth.getSnapshot.mockReturnValue({ status: "signed-in", user: { id: "ada" } })
  const fetch = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ id: "ada" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    })
  )
  vi.stubGlobal("fetch", fetch)

  await api.getProfile()

  expect(fetch).toHaveBeenCalledOnce()
  const [path, init] = fetch.mock.calls[0] as [string, RequestInit]
  expect(path).toBe("/api/profile")
  expect(new Headers(init.headers).get("Authorization")).toBe(
    "Bearer fresh-token"
  )
})

it("does not contact the backend without a verified token", async () => {
  auth.getAccessToken.mockRejectedValue(new Error("Sign in to continue."))
  const fetch = vi.fn()
  vi.stubGlobal("fetch", fetch)

  await expect(api.getProfile()).rejects.toThrow("Sign in to continue.")
  expect(fetch).not.toHaveBeenCalled()
})

it("locks the current account on a 401 without exposing backend detail", async () => {
  auth.getAccessToken.mockResolvedValue({ token: "stale-token", userId: "ada" })
  auth.getSnapshot.mockReturnValue({ status: "signed-in", user: { id: "ada" } })
  auth.signOut.mockResolvedValue(undefined)
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "private detail" } }), {
        status: 401,
      })
    )
  )

  await expect(api.getProfile()).rejects.toMatchObject({
    code: "authentication_required",
    status: 401,
    message: "Sign in again to continue.",
  })
  expect(auth.signOut).toHaveBeenCalledOnce()
})

it("does not sign out a newer session when an old token receives a 401", async () => {
  auth.getAccessToken
    .mockResolvedValueOnce({ token: "expired-token", userId: "ada" })
    .mockResolvedValueOnce({ token: "renewed-token", userId: "ada" })
  auth.getSnapshot.mockReturnValue({ status: "signed-in", user: { id: "ada" } })
  auth.signOut.mockResolvedValue(undefined)
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(null, { status: 401 }))
  )

  await expect(api.getProfile()).rejects.toMatchObject({
    code: "authentication_required",
    status: 401,
  })
  expect(auth.getAccessToken).toHaveBeenCalledTimes(2)
  expect(auth.signOut).not.toHaveBeenCalled()
})

it("does not sign out after cancellation during a rejected-token recheck", async () => {
  let resolveToken!: (value: { token: string; userId: string }) => void
  auth.getAccessToken
    .mockResolvedValueOnce({ token: "old-token", userId: "ada" })
    .mockReturnValueOnce(
      new Promise((resolve) => {
        resolveToken = resolve
      })
    )
  auth.getSnapshot.mockReturnValue({ status: "signed-in", user: { id: "ada" } })
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(null, { status: 401 }))
  )
  const controller = new AbortController()
  const rejected = expect(
    api.getProfile({ signal: controller.signal })
  ).rejects.toMatchObject({ name: "AbortError" })
  await vi.waitFor(() => expect(auth.getAccessToken).toHaveBeenCalledTimes(2))
  controller.abort()
  await rejected
  resolveToken({ token: "old-token", userId: "ada" })
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(auth.signOut).not.toHaveBeenCalled()
})

it("discards an old account response after the signed-in user changes", async () => {
  auth.getAccessToken.mockResolvedValue({ token: "ada-token", userId: "ada" })
  let currentUserId = "ada"
  auth.getSnapshot.mockImplementation(() => ({
    status: "signed-in",
    user: { id: currentUserId },
  }))
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async () => {
      currentUserId = "bob"
      return new Response(JSON.stringify({ id: "ada" }), { status: 200 })
    })
  )

  await expect(api.getProfile()).rejects.toMatchObject({
    code: "authentication_required",
    status: 401,
  })
  expect(auth.signOut).not.toHaveBeenCalled()
})

it("discards an old response after the same account signs out and back in", async () => {
  auth.getAccessToken.mockResolvedValue({ token: "ada-token", userId: "ada" })
  auth.getSnapshot.mockReturnValue({ status: "signed-in", user: { id: "ada" } })
  let epoch = 1
  auth.getSessionEpoch.mockImplementation(() => epoch)
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async () => {
      epoch = 2
      return new Response(JSON.stringify({ id: "ada" }), { status: 200 })
    })
  )

  await expect(api.getProfile()).rejects.toMatchObject({
    code: "authentication_required",
    status: 401,
  })
  expect(auth.signOut).not.toHaveBeenCalled()
})

it("discards an old account response when the user changes while parsing JSON", async () => {
  auth.getAccessToken.mockResolvedValue({ token: "ada-token", userId: "ada" })
  let currentUserId = "ada"
  auth.getSnapshot.mockImplementation(() => ({
    status: "signed-in",
    user: { id: currentUserId },
  }))
  let resolveJson!: (value: unknown) => void
  const pendingJson = new Promise<unknown>((resolve) => {
    resolveJson = resolve
  })
  const response = new Response("", { status: 200 })
  const json = vi.spyOn(response, "json").mockReturnValue(pendingJson)
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response))

  const request = api.getProfile()
  await vi.waitFor(() => expect(json).toHaveBeenCalledOnce())
  currentUserId = "bob"
  resolveJson({ id: "ada", display_name: "Private" })

  await expect(request).rejects.toMatchObject({
    code: "authentication_required",
    status: 401,
  })
  expect(auth.signOut).not.toHaveBeenCalled()
})

it("hides an old account error when the user changes while parsing JSON", async () => {
  auth.getAccessToken.mockResolvedValue({ token: "ada-token", userId: "ada" })
  let currentUserId = "ada"
  auth.getSnapshot.mockImplementation(() => ({
    status: "signed-in",
    user: { id: currentUserId },
  }))
  let resolveJson!: (value: unknown) => void
  const pendingJson = new Promise<unknown>((resolve) => {
    resolveJson = resolve
  })
  const response = new Response("", { status: 403 })
  const json = vi.spyOn(response, "json").mockReturnValue(pendingJson)
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response))

  const request = api.getProfile()
  await vi.waitFor(() => expect(json).toHaveBeenCalledOnce())
  currentUserId = "bob"
  resolveJson({ error: { code: "private_error", message: "Private detail" } })

  await expect(request).rejects.toMatchObject({
    code: "authentication_required",
    status: 401,
    message: "Sign in again to continue.",
  })
  expect(auth.signOut).not.toHaveBeenCalled()
})
