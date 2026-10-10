import type { Session, SupabaseClient, User } from "@supabase/supabase-js"

import {
  AuthConfigurationError,
  getSupabaseClient,
} from "@/lib/supabase-client"

export type AuthProvider = "google" | "github"

export interface AuthState {
  status:
    "loading" | "signed-out" | "signed-in" | "configuration-error" | "error"
  user: User | null
  error: string | null
  pendingProvider: AuthProvider | null
}

export interface AuthController {
  getSnapshot: () => AuthState
  subscribe: (listener: () => void) => () => void
  signIn: (provider: AuthProvider) => Promise<void>
  signOut: () => Promise<void>
  retry: () => Promise<void>
}

const emptyState: AuthState = {
  status: "loading",
  user: null,
  error: null,
  pendingProvider: null,
}

class AuthTimeoutError extends Error {}

class AuthCallbackError extends Error {
  readonly code: string

  constructor(code: string) {
    super("OAuth callback failed")
    this.code = code
  }
}

async function withDeadline<T>(operation: () => Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new AuthTimeoutError()), 15000)
      }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function failure(error: unknown): {
  status: "signed-out" | "error"
  message: string
} {
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? String(error.code)
      : ""
  if (code === "access_denied") {
    return {
      status: "signed-out",
      message: "Sign-in was cancelled or denied. Try again when you are ready.",
    }
  }
  if (
    (error instanceof Error &&
      error.name === "AuthPKCECodeVerifierMissingError") ||
    code === "bad_code_verifier"
  ) {
    return {
      status: "signed-out",
      message: "Start sign-in again in the same browser to finish securely.",
    }
  }
  if (
    ["flow_state_expired", "flow_state_not_found", "otp_expired"].includes(code)
  ) {
    return {
      status: "signed-out",
      message:
        "This sign-in link has expired or was already used. Please sign in again.",
    }
  }
  if (
    [
      "session_expired",
      "session_not_found",
      "bad_jwt",
      "user_not_found",
      "refresh_token_not_found",
    ].includes(code) ||
    (typeof error === "object" &&
      error !== null &&
      "status" in error &&
      (error.status === 401 || error.status === 403))
  ) {
    return {
      status: "signed-out",
      message: "Your session is no longer valid. Please sign in again.",
    }
  }
  if (error instanceof AuthTimeoutError) {
    return {
      status: "error",
      message: "Sign-in timed out. Check your connection and try again.",
    }
  }
  if (error instanceof AuthCallbackError) {
    return {
      status: "signed-out",
      message: "Sign-in could not be completed. Please try again.",
    }
  }
  return {
    status: "error",
    message:
      "We could not verify your sign-in. Check your connection and try again.",
  }
}

function consumeCallback(): {
  code: string | null
  flowId: string | null
  error: string | null
} {
  const url = new URL(window.location.href)
  const fragment = new URLSearchParams(url.hash.slice(1))
  const value = (key: string) => url.searchParams.get(key) ?? fragment.get(key)
  const callback = {
    code: value("code"),
    flowId: value("sb_flow_id"),
    error: value("error_code") ?? value("error"),
  }
  const keys = [
    "code",
    "error",
    "error_description",
    "error_code",
    "sb_flow_id",
  ]
  let changed = false
  for (const key of keys) {
    if (url.searchParams.has(key)) {
      url.searchParams.delete(key)
      changed = true
    }
  }
  if (keys.some((key) => fragment.has(key))) {
    for (const key of keys) fragment.delete(key)
    url.hash = fragment.toString()
    changed = true
  }
  if (changed) window.history.replaceState(window.history.state, "", url)
  return callback
}

export function createAuthController(
  client: SupabaseClient,
  navigate: (url: string) => void = (url) => window.location.assign(url)
): AuthController {
  let state = emptyState
  const listeners = new Set<() => void>()
  let sdkSubscription: { unsubscribe: () => void } | null = null
  let initialization: Promise<void> | null = null
  let callbackConsumed = false
  let version = 0
  let rejectLateAuthEvents = false
  let signingOut: Promise<void> | null = null
  let signOutRecoveryRequired = false

  const publish = (next: AuthState) => {
    state = next
    for (const listener of listeners) listener()
  }

  const publishFailure = (error: unknown, expectedVersion: number) => {
    if (version !== expectedVersion) return
    if (error instanceof AuthTimeoutError) {
      rejectLateAuthEvents = true
      version += 1
    }
    const result = failure(error)
    publish({ ...emptyState, status: result.status, error: result.message })
  }

  const validateSession = async (session: Session, expectedVersion: number) => {
    const { data, error } = await client.auth.getUser(session.access_token)
    if (error) throw error
    if (!data.user) throw new Error("Authenticated user missing")
    if (version !== expectedVersion) return
    publish({ ...emptyState, status: "signed-in", user: data.user })
  }

  const initialize = (): Promise<void> => {
    if (initialization) return initialization
    const expectedVersion = version
    initialization = withDeadline(async () => {
      let restoredSession: Session | null = null
      if (!callbackConsumed) {
        callbackConsumed = true
        const callback = consumeCallback()
        if (callback.error) throw new AuthCallbackError(callback.error)
        if (callback.code) {
          const { data, error } = await client.auth.exchangeCodeForSession(
            callback.code,
            callback.flowId ? { flowId: callback.flowId } : undefined
          )
          if (error) throw error
          restoredSession = data.session
        }
      }
      if (version !== expectedVersion) return
      if (!restoredSession) {
        const { data, error } = await client.auth.getSession()
        if (error) throw error
        restoredSession = data.session
      }
      if (version !== expectedVersion) return
      if (restoredSession)
        await validateSession(restoredSession, expectedVersion)
      else publish({ ...emptyState, status: "signed-out" })
    })
      .catch((error: unknown) => {
        publishFailure(error, expectedVersion)
      })
      .finally(() => {
        initialization = null
      })
    return initialization
  }

  const recoverFromBrowserBack = (event: PageTransitionEvent) => {
    if (!event.persisted || !state.pendingProvider) return
    version += 1
    rejectLateAuthEvents = true
    publish({
      ...emptyState,
      status: "signed-out",
      error: "Sign-in was interrupted. Choose a provider to try again.",
    })
  }

  const startSubscription = () => {
    const { data } = client.auth.onAuthStateChange((event, session) => {
      if (event === "INITIAL_SESSION") return
      if (event === "SIGNED_OUT" || !session) {
        version += 1
        rejectLateAuthEvents = true
        signOutRecoveryRequired = false
        publish({ ...emptyState, status: "signed-out" })
        return
      }
      if (rejectLateAuthEvents) return
      const expectedVersion = ++version
      if (state.status !== "signed-in" || state.user?.id !== session.user.id) {
        publish({ ...emptyState, status: "loading" })
      }
      // Supabase holds an auth lock during callbacks. Validation starts after
      // this synchronous callback returns, with the event's explicit token.
      queueMicrotask(() => {
        if (version !== expectedVersion) return
        void withDeadline(() =>
          validateSession(session, expectedVersion)
        ).catch((error: unknown) => {
          publishFailure(error, expectedVersion)
        })
      })
    })
    sdkSubscription = data.subscription
    window.addEventListener("pageshow", recoverFromBrowserBack)
  }

  const controller: AuthController = {
    getSnapshot: () => state,
    subscribe: (listener) => {
      const subscriptionListener = () => listener()
      listeners.add(subscriptionListener)
      if (listeners.size === 1) {
        startSubscription()
        void initialize()
      }
      return () => {
        listeners.delete(subscriptionListener)
        if (listeners.size === 0) {
          sdkSubscription?.unsubscribe()
          sdkSubscription = null
          window.removeEventListener("pageshow", recoverFromBrowserBack)
        }
      }
    },
    signIn: async (provider) => {
      if (state.pendingProvider || state.status === "signed-in") return
      const expectedVersion = ++version
      rejectLateAuthEvents = false
      signOutRecoveryRequired = false
      publish({
        ...emptyState,
        status: "signed-out",
        pendingProvider: provider,
      })
      try {
        const { data, error } = await withDeadline(() =>
          client.auth.signInWithOAuth({
            provider,
            options: {
              redirectTo: `${window.location.origin}/`,
              skipBrowserRedirect: true,
            },
          })
        )
        if (error) throw error
        if (!data.url) throw new Error("OAuth redirect missing")
        if (version !== expectedVersion) return
        navigate(data.url)
      } catch (error) {
        if (version !== expectedVersion) return
        if (error instanceof AuthTimeoutError) {
          rejectLateAuthEvents = true
          version += 1
        }
        publish({
          ...emptyState,
          status: "signed-out",
          error: failure(error).message,
        })
      }
    },
    signOut: () => {
      if (signingOut) return signingOut
      if (state.status !== "signed-in" && !signOutRecoveryRequired)
        return Promise.resolve()
      const previousState = state
      const expectedVersion = ++version
      rejectLateAuthEvents = true
      publish({ ...state, error: null, pendingProvider: null })
      signingOut = withDeadline(async () => {
        const { error } = await client.auth.signOut({ scope: "local" })
        if (error) throw error
      })
        .then(() => {
          if (version !== expectedVersion) return
          signOutRecoveryRequired = false
          publish({ ...emptyState, status: "signed-out" })
        })
        .catch((error: unknown) => {
          if (version !== expectedVersion) return
          if (error instanceof AuthTimeoutError) {
            version += 1
            signOutRecoveryRequired = true
            publish({
              ...emptyState,
              status: "error",
              error:
                "Sign-out timed out. Your workspace is locked. Try again to finish signing out.",
            })
            return
          }
          if (previousState.status === "signed-in") rejectLateAuthEvents = false
          publish({
            ...previousState,
            error: "We could not finish signing you out. Please try again.",
          })
        })
        .finally(() => {
          signingOut = null
        })
      return signingOut
    },
    retry: async () => {
      if (signOutRecoveryRequired) return controller.signOut()
      if (initialization) return initialization
      version += 1
      rejectLateAuthEvents = false
      publish({ ...emptyState, status: "loading" })
      return initialize()
    },
  }
  return controller
}

let controller: AuthController | undefined

export function getAuthController(): AuthController {
  if (controller) return controller
  try {
    controller = createAuthController(getSupabaseClient())
  } catch (error) {
    if (!(error instanceof AuthConfigurationError)) throw error
    const state: AuthState = {
      ...emptyState,
      status: "configuration-error",
      error: error.message,
    }
    controller = {
      getSnapshot: () => state,
      subscribe: () => () => {},
      signIn: async () => {},
      signOut: async () => {},
      retry: async () => {},
    }
  }
  return controller
}
