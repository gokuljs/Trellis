import type { Session, SupabaseClient, User } from "@supabase/supabase-js"

import { clearUserPendingRuns } from "@/lib/pending-run"

import {
  AuthConfigurationError,
  getSupabaseClient,
} from "@/lib/supabase-client"

export type AuthProvider = "google" | "github"

export type AuthCredential = {
  readonly token: string
  readonly userId: string
  readonly sessionEpoch: number
}

export class AuthAccessError extends Error {
  readonly code: "reauth_required" | "auth_unavailable"

  constructor(code: "reauth_required" | "auth_unavailable", message: string) {
    super(message)
    this.name = "AuthAccessError"
    this.code = code
  }
}

const expiredMessage = "Your session expired. Please sign in again"
const unavailableMessage =
  "Authentication is temporarily unavailable. Try again."

export interface AuthState {
  readonly status:
    "loading" | "signed-out" | "signed-in" | "configuration-error" | "error"
  readonly user: User | null
  readonly error: string | null
  readonly pendingProvider: AuthProvider | null
  readonly sessionEpoch: number
}

export interface AuthController {
  getSnapshot: () => AuthState
  getSessionEpoch: () => number
  getAccessToken: () => Promise<AuthCredential>
  isCredentialCurrent: (credential: AuthCredential) => boolean
  expireSession: (credential: AuthCredential) => Promise<void>
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
  sessionEpoch: 0,
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
  if (isInvalidSession(error)) {
    return {
      status: "signed-out",
      message: expiredMessage,
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

function isInvalidSession(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false
  const code = "code" in error ? String(error.code) : ""
  return (
    ("name" in error && error.name === "AuthSessionMissingError") ||
    [
      "session_expired",
      "session_not_found",
      "bad_jwt",
      "user_not_found",
      "refresh_token_not_found",
      "refresh_token_already_used",
      "reauth_required",
    ].includes(code) ||
    ("status" in error && (error.status === 401 || error.status === 403))
  )
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
  let accessGeneration = 0
  let currentAccessToken: string | null = null
  let validatedAccessToken: string | null = null
  let expiryCleanup: Promise<void> | null = null
  let expiryRecoveryRequired = false
  let pendingUserId: string | null = null

  const forgetPendingRuns = () => {
    if (pendingUserId) clearUserPendingRuns(pendingUserId)
    pendingUserId = null
  }

  const publish = (next: AuthState) => {
    if (
      state.status === "signed-in" &&
      (next.status !== "signed-in" || state.user?.id !== next.user?.id)
    ) {
      accessGeneration += 1
    }
    if (next.status !== "signed-in") {
      currentAccessToken = null
      validatedAccessToken = null
    }
    state = { ...next, sessionEpoch: accessGeneration }
    for (const listener of listeners) listener()
  }

  const isCredentialCurrent = (credential: AuthCredential) =>
    state.status === "signed-in" &&
    state.user?.id === credential.userId &&
    accessGeneration === credential.sessionEpoch

  const cleanupExpiredSession = (): Promise<void> => {
    if (expiryCleanup) return expiryCleanup
    const expectedVersion = version
    const cleanup = withDeadline(async () => {
      const { error } = await client.auth.signOut({ scope: "local" })
      if (error) throw error
    })
      .then(() => {
        if (version === expectedVersion) expiryRecoveryRequired = false
      })
      .catch(() => {
        // Cleanup cannot restore credentials that have already been rejected.
      })
      .finally(() => {
        if (expiryCleanup === cleanup) expiryCleanup = null
      })
    expiryCleanup = cleanup
    return cleanup
  }

  const expireCurrentSession = (): Promise<void> => {
    forgetPendingRuns()
    version += 1
    rejectLateAuthEvents = true
    signOutRecoveryRequired = false
    expiryRecoveryRequired = true
    publish({ ...emptyState, status: "signed-out", error: expiredMessage })
    return cleanupExpiredSession()
  }

  const publishFailure = (error: unknown, expectedVersion: number) => {
    if (version !== expectedVersion) return
    if (isInvalidSession(error)) {
      void expireCurrentSession()
      return
    }
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
    if (!data.user || data.user.id !== session.user.id)
      throw new AuthAccessError("reauth_required", expiredMessage)
    if (version !== expectedVersion) return
    if (pendingUserId && pendingUserId !== data.user.id) forgetPendingRuns()
    pendingUserId = data.user.id
    currentAccessToken = session.access_token
    validatedAccessToken = session.access_token
    publish({ ...emptyState, status: "signed-in", user: data.user })
  }

  const initialize = (): Promise<void> => {
    if (state.status === "signed-out" && state.error === expiredMessage)
      return Promise.resolve()
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
        forgetPendingRuns()
        if (state.status === "signed-out" && state.error === expiredMessage)
          return
        if (state.status === "signed-in" && !signingOut) {
          void expireCurrentSession()
          return
        }
        version += 1
        rejectLateAuthEvents = true
        signOutRecoveryRequired = false
        publish({ ...emptyState, status: "signed-out" })
        return
      }
      if (rejectLateAuthEvents) return
      const expectedVersion = ++version
      if (
        state.status !== "signed-in" ||
        state.user?.id !== session.user.id ||
        (event === "SIGNED_IN" && validatedAccessToken !== session.access_token)
      ) {
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
    getSessionEpoch: () => accessGeneration,
    isCredentialCurrent,
    expireSession: (credential) => {
      if (
        !isCredentialCurrent(credential) ||
        currentAccessToken !== credential.token
      )
        return Promise.resolve()
      return expireCurrentSession()
    },
    getAccessToken: async () => {
      const user = state.status === "signed-in" ? state.user : null
      if (!user) throw new AuthAccessError("reauth_required", expiredMessage)
      const expectedGeneration = accessGeneration
      const expectedVersion = version
      const current = () =>
        state.status === "signed-in" &&
        state.user?.id === user.id &&
        accessGeneration === expectedGeneration
      let result: Awaited<ReturnType<typeof client.auth.getSession>>
      try {
        result = await withDeadline(() => client.auth.getSession())
      } catch (error) {
        if (!current())
          throw new AuthAccessError("reauth_required", expiredMessage)
        if (isInvalidSession(error) && version === expectedVersion) {
          void expireCurrentSession()
          throw new AuthAccessError("reauth_required", expiredMessage)
        }
        throw new AuthAccessError("auth_unavailable", unavailableMessage)
      }
      if (!current())
        throw new AuthAccessError("reauth_required", expiredMessage)
      const { data, error } = result
      if (error) {
        if (isInvalidSession(error) && version === expectedVersion) {
          void expireCurrentSession()
          throw new AuthAccessError("reauth_required", expiredMessage)
        }
        throw new AuthAccessError("auth_unavailable", unavailableMessage)
      }
      if (!data.session?.access_token || data.session.user.id !== user.id) {
        if (version === expectedVersion) void expireCurrentSession()
        throw new AuthAccessError("reauth_required", expiredMessage)
      }
      if (version === expectedVersion)
        currentAccessToken = data.session.access_token
      return {
        token: data.session.access_token,
        userId: user.id,
        sessionEpoch: expectedGeneration,
      }
    },
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
      if (expiryRecoveryRequired || expiryCleanup) {
        publish({ ...state, pendingProvider: provider })
        await cleanupExpiredSession()
        if (expiryRecoveryRequired) {
          publish({ ...state, pendingProvider: null })
          return
        }
      }
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
      publish({ ...emptyState, status: "loading" })
      signingOut = withDeadline(async () => {
        const { error } = await client.auth.signOut({ scope: "local" })
        if (error) throw error
      })
        .then(() => {
          if (version !== expectedVersion) return
          signOutRecoveryRequired = false
          forgetPendingRuns()
          publish({ ...emptyState, status: "signed-out" })
        })
        .catch((error: unknown) => {
          if (version !== expectedVersion) return
          if (isInvalidSession(error)) return expireCurrentSession()
          if (error instanceof AuthTimeoutError) {
            version += 1
            signOutRecoveryRequired = true
            publish({
              ...emptyState,
              status: "error",
              error:
                "Sign-out timed out. Your chats are locked. Try again to finish signing out.",
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
      if (expiryRecoveryRequired || expiryCleanup) {
        await cleanupExpiredSession()
        return
      }
      if (state.status === "signed-out" && state.error === expiredMessage)
        return
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
      getSessionEpoch: () => 0,
      getAccessToken: async () => {
        throw new AuthAccessError(
          "auth_unavailable",
          state.error ?? unavailableMessage
        )
      },
      isCredentialCurrent: () => false,
      expireSession: async () => {},
      subscribe: () => () => {},
      signIn: async () => {},
      signOut: async () => {},
      retry: async () => {},
    }
  }
  return controller
}
