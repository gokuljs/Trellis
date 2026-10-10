import { createClient, type SupabaseClient } from "@supabase/supabase-js"

interface SupabaseEnvironment {
  VITE_SUPABASE_URL?: string
  VITE_SUPABASE_PUBLISHABLE_KEY?: string
}

export class AuthConfigurationError extends Error {
  constructor() {
    super(
      "Configure VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY to enable sign-in."
    )
    this.name = "AuthConfigurationError"
  }
}

export function createSupabaseClient(env: SupabaseEnvironment): SupabaseClient {
  const key = env.VITE_SUPABASE_PUBLISHABLE_KEY?.trim()
  let url: URL
  try {
    url = new URL(env.VITE_SUPABASE_URL?.trim() ?? "")
  } catch {
    throw new AuthConfigurationError()
  }

  if (
    url.protocol !== "https:" ||
    url.hostname === "localhost" ||
    url.hostname === "your-project.supabase.co" ||
    url.username ||
    url.password ||
    url.port ||
    url.pathname !== "/" ||
    url.search ||
    url.hash ||
    !key ||
    !/^sb_publishable_[A-Za-z0-9_-]+$/.test(key) ||
    key === "sb_publishable_replace_me"
  ) {
    throw new AuthConfigurationError()
  }

  return createClient(url.origin, key, {
    auth: {
      flowType: "pkce",
      detectSessionInUrl: false,
      persistSession: true,
      autoRefreshToken: true,
    },
  })
}

let client: SupabaseClient | undefined

export function getSupabaseClient(): SupabaseClient {
  client ??= createSupabaseClient(import.meta.env)
  return client
}
