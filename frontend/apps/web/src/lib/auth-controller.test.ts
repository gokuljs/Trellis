import type {
  AuthChangeEvent,
  Session,
  SupabaseClient,
  User,
} from "@supabase/supabase-js"
import { afterEach, describe, expect, it, vi } from "vitest"

import { createAuthController, getAuthController } from "@/lib/auth-controller"
import { createSupabaseClient } from "@/lib/supabase-client"

const user: User = {
  id: "verified-user",
  aud: "authenticated",
  role: "authenticated",
  email: "gokul@example.com",
  app_metadata: { provider: "google", providers: ["google"] },
  user_metadata: { name: "Gokul" },
  identities: [],
  created_at: "2026-10-09T00:00:00Z",
  updated_at: "2026-10-09T00:00:00Z",
}

const session: Session = {
  access_token: "stored-access-token",
  refresh_token: "stored-refresh-token",
  expires_in: 3600,
  expires_at: 4102444800,
  token_type: "bearer",
  user: { ...user, id: "untrusted-cached-user" },
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function fakeAuthClient() {
  const callbacks = new Set<
    (event: AuthChangeEvent, session: Session | null) => void
  >()
  const auth = {
    getSession: vi.fn(async () => ({
      data: { session: null as Session | null },
      error: null,
    })),
    getUser: vi.fn<SupabaseClient["auth"]["getUser"]>().mockResolvedValue({
      data: { user },
      error: null,
    }),
    exchangeCodeForSession: vi
      .fn<SupabaseClient["auth"]["exchangeCodeForSession"]>()
      .mockResolvedValue({
        data: { session, user },
        error: null,
      }),
    signInWithOAuth: vi
      .fn<SupabaseClient["auth"]["signInWithOAuth"]>()
      .mockResolvedValue({
        data: {
          provider: "google" as const,
          url: "https://provider.example/login",
        },
        error: null,
      }),
    onAuthStateChange: (
      callback: (event: AuthChangeEvent, session: Session | null) => void
    ) => {
      callbacks.add(callback)
      return {
        data: {
          subscription: {
            id: "test-subscription",
            callback,
            unsubscribe: () => callbacks.delete(callback),
          },
        },
      }
    },
  }
  return {
    client: { auth } as unknown as SupabaseClient,
    auth,
    emit: (event: AuthChangeEvent, nextSession: Session | null) => {
      for (const callback of callbacks) callback(event, nextSession)
    },
    subscriptionCount: () => callbacks.size,
  }
}

const cleanups: (() => void)[] = []
const sdkClients: SupabaseClient[] = []

afterEach(async () => {
  for (const cleanup of cleanups) cleanup()
  cleanups.length = 0
  for (const client of sdkClients) await client.auth.stopAutoRefresh()
  sdkClients.length = 0
  localStorage.clear()
  window.history.replaceState({}, "", "/")
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe("authentication controller", () => {
  it("restores signed-out state after the first subscriber mounts", async () => {
    const { client } = fakeAuthClient()
    const controller = createAuthController(client)

    expect(controller.getSnapshot().status).toBe("loading")
    cleanups.push(controller.subscribe(() => {}))

    await vi.waitFor(() => {
      expect(controller.getSnapshot()).toEqual({
        status: "signed-out",
        user: null,
        error: null,
        pendingProvider: null,
      })
    })
  })

  it("validates the access token before trusting a saved user", async () => {
    const { client, auth } = fakeAuthClient()
    auth.getSession.mockResolvedValue({ data: { session }, error: null })
    const validation = deferred<{ data: { user: User }; error: null }>()
    auth.getUser.mockReturnValue(validation.promise)
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))

    await vi.waitFor(() =>
      expect(auth.getUser).toHaveBeenCalledWith("stored-access-token")
    )
    expect(controller.getSnapshot().status).toBe("loading")
    validation.resolve({ data: { user }, error: null })

    await vi.waitFor(() =>
      expect(controller.getSnapshot().user?.id).toBe("verified-user")
    )
    expect(controller.getSnapshot().status).toBe("signed-in")
  })

  it("does not let a late restore overwrite sign-out", async () => {
    const { client, auth, emit } = fakeAuthClient()
    const restoration = deferred<{ data: { session: Session }; error: null }>()
    auth.getSession.mockReturnValue(restoration.promise)
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))

    emit("SIGNED_OUT", null)
    restoration.resolve({ data: { session }, error: null })
    await Promise.resolve()
    await Promise.resolve()

    expect(controller.getSnapshot().status).toBe("signed-out")
    expect(controller.getSnapshot().user).toBeNull()
  })

  it("ignores an old callback's sign-in event after sign-out", async () => {
    const { client, emit } = fakeAuthClient()
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))
    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )

    emit("SIGNED_OUT", null)
    emit("SIGNED_IN", session)
    await Promise.resolve()
    await Promise.resolve()

    expect(controller.getSnapshot().status).toBe("signed-out")
    expect(controller.getSnapshot().user).toBeNull()
  })

  it("exchanges and validates a PKCE callback through the real SDK exactly once", async () => {
    const requests: { url: string; options?: RequestInit }[] = []
    vi.stubGlobal(
      "fetch",
      async (input: RequestInfo | URL, options?: RequestInit) => {
        const url = String(input)
        requests.push({ url, options })
        if (
          url ===
          "https://successful-callback.supabase.co/auth/v1/token?grant_type=pkce"
        ) {
          return new Response(JSON.stringify({ ...session, user }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        }
        if (url === "https://successful-callback.supabase.co/auth/v1/user") {
          return new Response(JSON.stringify(user), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        }
        throw new Error("Unexpected external request")
      }
    )
    const flowId = "successful-flow-1234"
    localStorage.setItem(
      "sb-successful-callback-auth-token-flow-successful-flow-1234-code-verifier",
      JSON.stringify("secure-verifier")
    )
    window.history.replaceState(
      {},
      "",
      `/?code=successful-code&sb_flow_id=${flowId}`
    )
    const client = createSupabaseClient({
      VITE_SUPABASE_URL: "https://successful-callback.supabase.co",
      VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_successful_callback",
    })
    sdkClients.push(client)
    const controller = createAuthController(client)
    const unsubscribe = controller.subscribe(() => {})
    unsubscribe()
    cleanups.push(controller.subscribe(() => {}))

    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-in")
    )

    const exchanges = requests.filter((request) =>
      request.url.includes("/token?")
    )
    expect(exchanges).toHaveLength(1)
    expect(JSON.parse(String(exchanges[0]?.options?.body))).toEqual({
      auth_code: "successful-code",
      code_verifier: "secure-verifier",
    })
    expect(
      requests.filter((request) => request.url.endsWith("/user"))
    ).toHaveLength(1)
    expect(controller.getSnapshot().user?.id).toBe("verified-user")
    expect(window.location.search).toBe("")
  })

  it("reports an expired callback without exposing server details", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(
          JSON.stringify({
            error_code: "flow_state_expired",
            message: "Sensitive callback trace from provider",
          }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        )
    )
    localStorage.setItem(
      "sb-expired-callback-auth-token-code-verifier",
      JSON.stringify("expired-verifier")
    )
    window.history.replaceState({}, "", "/?code=expired-code")
    const client = createSupabaseClient({
      VITE_SUPABASE_URL: "https://expired-callback.supabase.co",
      VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_expired_callback",
    })
    sdkClients.push(client)
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))

    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )
    expect(controller.getSnapshot().error).toMatch(/expired|already used/i)
    expect(controller.getSnapshot().error).not.toContain("Sensitive callback")
  })

  it("keeps a verified workspace mounted during token refresh", async () => {
    const { client, auth, emit } = fakeAuthClient()
    auth.getSession.mockResolvedValue({ data: { session }, error: null })
    const controller = createAuthController(client)
    const observedStatuses: string[] = []
    cleanups.push(
      controller.subscribe(() =>
        observedStatuses.push(controller.getSnapshot().status)
      )
    )
    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-in")
    )
    observedStatuses.length = 0

    const refreshedSession = {
      ...session,
      user,
      access_token: "refreshed-access-token",
    }
    emit("TOKEN_REFRESHED", refreshedSession)

    expect(controller.getSnapshot().status).toBe("signed-in")
    await vi.waitFor(() =>
      expect(auth.getUser).toHaveBeenCalledWith("refreshed-access-token")
    )
    expect(observedStatuses).not.toContain("loading")
  })

  it("ignores a superseded user validation", async () => {
    const { client, auth, emit } = fakeAuthClient()
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))
    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )
    const firstValidation = deferred<{ data: { user: User }; error: null }>()
    auth.getUser.mockReturnValueOnce(firstValidation.promise)

    emit("SIGNED_IN", session)
    await vi.waitFor(() =>
      expect(auth.getUser).toHaveBeenCalledWith("stored-access-token")
    )
    emit("SIGNED_OUT", null)
    firstValidation.resolve({ data: { user }, error: null })
    await Promise.resolve()
    await Promise.resolve()

    expect(controller.getSnapshot().status).toBe("signed-out")
    expect(controller.getSnapshot().user).toBeNull()
  })

  it("shares restoration across StrictMode resubscription and releases the SDK listener", async () => {
    const { client, auth, subscriptionCount } = fakeAuthClient()
    const restoration = deferred<{ data: { session: null }; error: null }>()
    auth.getSession.mockReturnValue(restoration.promise)
    const controller = createAuthController(client)
    const unsubscribe = controller.subscribe(() => {})
    expect(subscriptionCount()).toBe(1)
    unsubscribe()
    expect(subscriptionCount()).toBe(0)
    cleanups.push(controller.subscribe(() => {}))
    expect(subscriptionCount()).toBe(1)
    restoration.resolve({ data: { session: null }, error: null })

    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )
    expect(auth.getSession).toHaveBeenCalledTimes(1)
  })

  it.each(["google", "github"] as const)(
    "signs in with %s using only the app root redirect",
    async (provider) => {
      const { client, auth } = fakeAuthClient()
      window.history.replaceState(
        {},
        "",
        "/?return_to=https://attacker.example/"
      )
      const navigate = vi.fn()
      const controller = createAuthController(client, navigate)
      cleanups.push(controller.subscribe(() => {}))
      await vi.waitFor(() =>
        expect(controller.getSnapshot().status).toBe("signed-out")
      )

      await controller.signIn(provider)

      expect(auth.signInWithOAuth).toHaveBeenCalledWith({
        provider,
        options: {
          redirectTo: `${window.location.origin}/`,
          skipBrowserRedirect: true,
        },
      })
      expect(controller.getSnapshot().pendingProvider).toBe(provider)
      expect(navigate).toHaveBeenCalledExactlyOnceWith(
        "https://provider.example/login"
      )
    }
  )

  it("cleans a denied callback without exposing its error description", async () => {
    const { client } = fakeAuthClient()
    window.history.replaceState(
      {},
      "",
      "/?view=chat&error=access_denied&error_description=secret-provider-detail#tab=profile"
    )
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))

    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )
    expect(controller.getSnapshot().error).toMatch(/cancelled|denied/i)
    expect(controller.getSnapshot().error).not.toContain(
      "secret-provider-detail"
    )
    expect(window.location.search).toBe("?view=chat")
    expect(window.location.hash).toBe("#tab=profile")
  })

  it("exchanges one callback with its flow ID and preserves unrelated URL state", async () => {
    const { client, auth } = fakeAuthClient()
    window.history.replaceState(
      {},
      "",
      "/?view=chat&code=oauth-code&sb_flow_id=flow-123456#tab=profile"
    )
    const controller = createAuthController(client)
    const unsubscribe = controller.subscribe(() => {})
    unsubscribe()
    cleanups.push(controller.subscribe(() => {}))

    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-in")
    )
    expect(auth.exchangeCodeForSession).toHaveBeenCalledExactlyOnceWith(
      "oauth-code",
      { flowId: "flow-123456" }
    )
    expect(window.location.search).toBe("?view=chat")
    expect(window.location.hash).toBe("#tab=profile")
  })

  it("cleans callback parameters from an OAuth fragment", async () => {
    const { client } = fakeAuthClient()
    window.history.replaceState(
      {},
      "",
      "/?view=chat#code=oauth-code&sb_flow_id=flow-123456&tab=profile"
    )
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))

    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-in")
    )
    expect(window.location.search).toBe("?view=chat")
    expect(window.location.hash).toBe("#tab=profile")
  })

  it("reports a missing PKCE verifier through the real SDK", async () => {
    const client = createSupabaseClient({
      VITE_SUPABASE_URL: "https://missing-verifier.supabase.co",
      VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_test_missing_verifier",
    })
    sdkClients.push(client)
    window.history.replaceState({}, "", "/?code=unusable-code")
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))

    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )
    expect(controller.getSnapshot().error).toMatch(
      /same browser|start.*sign-in|sign in again/i
    )
    expect(window.location.search).toBe("")
  })

  it("ends a stalled restoration after 15 seconds and ignores its late result", async () => {
    vi.useFakeTimers()
    const { client, auth } = fakeAuthClient()
    const restoration = deferred<{ data: { session: Session }; error: null }>()
    auth.getSession.mockReturnValue(restoration.promise)
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))

    await vi.advanceTimersByTimeAsync(15000)

    expect(controller.getSnapshot().status).toBe("error")
    expect(controller.getSnapshot().error).toMatch(/timed out|connection/i)
    restoration.resolve({ data: { session }, error: null })
    await vi.advanceTimersByTimeAsync(0)
    expect(controller.getSnapshot().status).toBe("error")
    expect(controller.getSnapshot().user).toBeNull()
  })

  it("retries a failed restoration", async () => {
    const { client, auth } = fakeAuthClient()
    auth.getSession.mockRejectedValueOnce(
      new TypeError("Network detail must remain private")
    )
    const controller = createAuthController(client)
    cleanups.push(controller.subscribe(() => {}))
    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("error")
    )
    expect(controller.getSnapshot().error).not.toContain("Network detail")

    await controller.retry()

    expect(controller.getSnapshot().status).toBe("signed-out")
    expect(controller.getSnapshot().error).toBeNull()
  })

  it("releases the provider action when OAuth fails", async () => {
    const { client, auth } = fakeAuthClient()
    auth.signInWithOAuth.mockRejectedValueOnce(
      new TypeError("Private provider configuration")
    )
    const controller = createAuthController(client, () => {})
    cleanups.push(controller.subscribe(() => {}))
    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )

    await controller.signIn("github")

    expect(controller.getSnapshot().status).toBe("signed-out")
    expect(controller.getSnapshot().pendingProvider).toBeNull()
    expect(controller.getSnapshot().error).toBeTruthy()
    expect(controller.getSnapshot().error).not.toContain("Private provider")
    await controller.signIn("google")
    expect(controller.getSnapshot().pendingProvider).toBe("google")
  })

  it("does not navigate when an old provider result arrives after its timeout", async () => {
    vi.useFakeTimers()
    const { client, auth } = fakeAuthClient()
    const providerResult =
      deferred<Awaited<ReturnType<SupabaseClient["auth"]["signInWithOAuth"]>>>()
    auth.signInWithOAuth.mockReturnValueOnce(providerResult.promise)
    const navigate = vi.fn()
    const controller = createAuthController(client, navigate)
    cleanups.push(controller.subscribe(() => {}))
    await vi.advanceTimersByTimeAsync(0)
    const signingIn = controller.signIn("google")

    await vi.advanceTimersByTimeAsync(15000)
    await signingIn
    expect(controller.getSnapshot().pendingProvider).toBeNull()
    expect(controller.getSnapshot().error).toMatch(/timed out/i)
    providerResult.resolve({
      data: { provider: "google", url: "https://google.example/old-login" },
      error: null,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(navigate).not.toHaveBeenCalled()
    expect(controller.getSnapshot().error).toMatch(/timed out/i)
  })

  it("navigates only for the newest provider when a pending request is superseded", async () => {
    const { client, auth, emit } = fakeAuthClient()
    const oldProviderResult =
      deferred<Awaited<ReturnType<SupabaseClient["auth"]["signInWithOAuth"]>>>()
    auth.signInWithOAuth.mockReturnValueOnce(oldProviderResult.promise)
    auth.signInWithOAuth.mockResolvedValueOnce({
      data: { provider: "github", url: "https://github.example/new-login" },
      error: null,
    })
    const navigate = vi.fn()
    const controller = createAuthController(client, navigate)
    cleanups.push(controller.subscribe(() => {}))
    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )
    const oldSignIn = controller.signIn("google")
    await vi.waitFor(() =>
      expect(auth.signInWithOAuth).toHaveBeenCalledTimes(1)
    )
    emit("SIGNED_OUT", null)

    await controller.signIn("github")
    oldProviderResult.resolve({
      data: { provider: "google", url: "https://google.example/old-login" },
      error: null,
    })
    await oldSignIn

    expect(navigate).toHaveBeenCalledExactlyOnceWith(
      "https://github.example/new-login"
    )
    expect(controller.getSnapshot().pendingProvider).toBe("github")
  })

  it("navigates to the real SDK's PKCE authorization URL after successful generation", async () => {
    const client = createSupabaseClient({
      VITE_SUPABASE_URL: "https://generated-provider.supabase.co",
      VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_generated_provider",
    })
    sdkClients.push(client)
    const urls: string[] = []
    const controller = createAuthController(client, (url) => urls.push(url))

    await controller.signIn("github")

    expect(urls).toHaveLength(1)
    const url = new URL(urls[0] ?? "")
    expect(url.origin).toBe("https://generated-provider.supabase.co")
    expect(url.pathname).toBe("/auth/v1/authorize")
    expect(url.searchParams.get("provider")).toBe("github")
    expect(url.searchParams.get("code_challenge")).toBeTruthy()
    expect(new URL(url.searchParams.get("redirect_to") ?? "").origin).toBe(
      window.location.origin
    )
  })

  it("keeps one SDK subscription until every subscriber leaves", async () => {
    const { client, subscriptionCount, emit } = fakeAuthClient()
    const controller = createAuthController(client)
    const unsubscribeFirst = controller.subscribe(() => {})
    const unsubscribeSecond = controller.subscribe(() => {})
    expect(subscriptionCount()).toBe(1)
    unsubscribeFirst()
    expect(subscriptionCount()).toBe(1)
    await vi.waitFor(() =>
      expect(controller.getSnapshot().status).toBe("signed-out")
    )
    unsubscribeSecond()
    expect(subscriptionCount()).toBe(0)

    emit("SIGNED_IN", session)
    await Promise.resolve()

    expect(controller.getSnapshot().status).toBe("signed-out")
  })

  it("treats repeated subscriber callbacks as separate subscriptions", () => {
    const { client, subscriptionCount } = fakeAuthClient()
    const controller = createAuthController(client)
    const listener = () => {}
    const unsubscribeFirst = controller.subscribe(listener)
    const unsubscribeSecond = controller.subscribe(listener)
    cleanups.push(unsubscribeFirst, unsubscribeSecond)

    expect(subscriptionCount()).toBe(1)
    unsubscribeFirst()
    expect(subscriptionCount()).toBe(1)
    unsubscribeSecond()
    expect(subscriptionCount()).toBe(0)
  })

  it("returns a configuration error store when browser configuration is absent", async () => {
    vi.stubEnv("VITE_SUPABASE_URL", "")
    vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "")
    const { getAuthController: getFreshController } =
      await import("@/lib/auth-controller")
    const controller = getFreshController()

    expect(controller.getSnapshot().status).toBe("configuration-error")
    expect(controller.getSnapshot().user).toBeNull()
    expect(getFreshController()).toBe(controller)
    expect(getAuthController).toBeTypeOf("function")
  })
})
