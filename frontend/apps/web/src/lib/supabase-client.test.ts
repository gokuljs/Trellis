import { afterEach, describe, expect, it, vi } from "vitest"

import {
  AuthConfigurationError,
  createSupabaseClient,
  getSupabaseClient,
} from "@/lib/supabase-client"

const projectUrl = "https://trellis-auth-test.supabase.co"
const publishableKey = "sb_publishable_trellis_auth_test"
const clients: ReturnType<typeof createSupabaseClient>[] = []

function clientWith(
  env = {
    VITE_SUPABASE_URL: projectUrl,
    VITE_SUPABASE_PUBLISHABLE_KEY: publishableKey,
  }
) {
  const client = createSupabaseClient(env)
  clients.push(client)
  return client
}

afterEach(() => {
  for (const client of clients) client.auth.stopAutoRefresh()
  clients.length = 0
  localStorage.clear()
  window.history.replaceState({}, "", "/")
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe("Supabase configuration", () => {
  it.each([
    undefined,
    "",
    "not-a-url",
    "http://trellis-auth-test.supabase.co",
    "https://localhost:54321",
    "https://your-project.supabase.co",
    "https://trellis-auth-test.supabase.co/auth/v1",
    "https://trellis-auth-test.supabase.co?key=secret",
    "https://trellis-auth-test.supabase.co#secret",
    "https://user:secret@trellis-auth-test.supabase.co",
  ])("rejects an invalid project URL %s before creating a client", (url) => {
    expect(() =>
      createSupabaseClient({
        VITE_SUPABASE_URL: url,
        VITE_SUPABASE_PUBLISHABLE_KEY: publishableKey,
      })
    ).toThrow(AuthConfigurationError)
  })

  it.each([
    undefined,
    "",
    "sb_publishable_",
    "sb_publishable_replace_me",
    "sb_secret_private_service_key",
    "legacy.jwt.service-role-key",
  ])("rejects a non-publishable browser key %s", (key) => {
    expect(() =>
      createSupabaseClient({
        VITE_SUPABASE_URL: projectUrl,
        VITE_SUPABASE_PUBLISHABLE_KEY: key,
      })
    ).toThrow(AuthConfigurationError)
  })

  it("keeps rejected configuration values out of errors", () => {
    const secret = "sb_secret_do_not_disclose"
    expect(() =>
      createSupabaseClient({
        VITE_SUPABASE_URL: projectUrl,
        VITE_SUPABASE_PUBLISHABLE_KEY: secret,
      })
    ).not.toThrow(secret)
  })

  it.each(["google", "github"] as const)(
    "starts %s OAuth with a PKCE challenge and persisted verifier",
    async (provider) => {
      const providerProjectUrl = `https://${provider}-auth-test.supabase.co`
      const client = clientWith({
        VITE_SUPABASE_URL: providerProjectUrl,
        VITE_SUPABASE_PUBLISHABLE_KEY: publishableKey,
      })
      const { data, error } = await client.auth.signInWithOAuth({
        provider,
        options: {
          redirectTo: "http://localhost:3000/",
          skipBrowserRedirect: true,
        },
      })

      expect(error).toBeNull()
      const authorizationUrl = new URL(data.url ?? "")
      expect(authorizationUrl.origin).toBe(providerProjectUrl)
      expect(authorizationUrl.pathname).toBe("/auth/v1/authorize")
      expect(authorizationUrl.searchParams.get("provider")).toBe(provider)
      const redirect = new URL(
        authorizationUrl.searchParams.get("redirect_to") ?? ""
      )
      expect(redirect.origin).toBe("http://localhost:3000")
      expect(redirect.pathname).toBe("/")
      expect(authorizationUrl.searchParams.get("code_challenge")).toBeTruthy()
      expect(
        localStorage.getItem(
          `sb-${provider}-auth-test-auth-token-code-verifier`
        )
      ).toBeTruthy()
    }
  )

  it("does not exchange a callback until the app handles it explicitly", async () => {
    const verifierKey = "sb-callback-auth-test-auth-token-code-verifier"
    const savedVerifier = JSON.stringify("pending-pkce-verifier")
    localStorage.setItem(verifierKey, savedVerifier)
    window.history.replaceState({}, "", "/?code=pending-oauth-code")
    const fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: "invalid_grant",
          error_description: "This OAuth code has already been processed.",
        }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      )
    )
    vi.stubGlobal("fetch", fetch)
    const client = clientWith({
      VITE_SUPABASE_URL: "https://callback-auth-test.supabase.co",
      VITE_SUPABASE_PUBLISHABLE_KEY: publishableKey,
    })

    const { data, error } = await client.auth.getSession()

    expect(error).toBeNull()
    expect(data.session).toBeNull()
    expect(fetch).not.toHaveBeenCalled()
    expect(localStorage.getItem(verifierKey)).toBe(savedVerifier)
  })

  it("supports a hosted HTTPS custom Supabase domain", async () => {
    const client = clientWith({
      VITE_SUPABASE_URL: "https://auth.trellis.example/",
      VITE_SUPABASE_PUBLISHABLE_KEY: publishableKey,
    })
    const { data } = await client.auth.signInWithOAuth({
      provider: "google",
      options: { skipBrowserRedirect: true },
    })

    expect(new URL(data.url ?? "").origin).toBe("https://auth.trellis.example")
  })

  it("reuses one lazily initialized client for the application", () => {
    vi.stubEnv("VITE_SUPABASE_URL", projectUrl)
    vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", publishableKey)

    const firstClient = getSupabaseClient()
    clients.push(firstClient)

    expect(getSupabaseClient()).toBe(firstClient)
  })
})
