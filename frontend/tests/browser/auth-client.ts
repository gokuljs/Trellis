import type { Session, SupabaseClient, User } from "@supabase/supabase-js"

const users = {
  alice: "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5",
  bob: "d7a9cd14-6abd-42c4-b46d-2f5bb82e7231",
}

function session(): Session | null {
  const name = localStorage.getItem("trellis.test-user") ?? "alice"
  if (name !== "alice" && name !== "bob") return null
  const user: User = {
    id: users[name],
    aud: "authenticated",
    role: "authenticated",
    email: `${name}@example.test`,
    created_at: "2026-10-18T00:00:00Z",
    app_metadata: { provider: "google" },
    user_metadata: { full_name: name === "alice" ? "Alice" : "Bob" },
  }
  return {
    access_token: name,
    token_type: "bearer",
    refresh_token: "disposable-test-token",
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user,
  }
}

const client = {
  auth: {
    getSession: async () => ({ data: { session: session() }, error: null }),
    getUser: async () => ({
      data: { user: session()?.user ?? null },
      error: null,
    }),
    onAuthStateChange: () => ({
      data: { subscription: { unsubscribe: () => undefined } },
    }),
    signOut: async () => {
      localStorage.setItem("trellis.test-user", "signed-out")
      return { error: null }
    },
    signInWithOAuth: async () => {
      localStorage.setItem("trellis.test-user", "alice")
      return { data: { url: `${window.location.origin}/` }, error: null }
    },
  },
} as unknown as SupabaseClient

export class AuthConfigurationError extends Error {}

export function getSupabaseClient(): SupabaseClient {
  return client
}
