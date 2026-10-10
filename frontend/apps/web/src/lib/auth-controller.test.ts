import type { Session, SupabaseClient, User } from "@supabase/supabase-js"
import { afterEach, expect, it, vi } from "vitest"

import { createAuthController } from "@/lib/auth-controller"

const ada: User = {
  id: "ada-id",
  aud: "authenticated",
  email: "ada@example.com",
  app_metadata: { provider: "google" },
  user_metadata: {},
  created_at: "2026-10-10T00:00:00Z",
}

const bob: User = { ...ada, id: "bob-id", email: "bob@example.com" }

function session(user: User, accessToken: string): Session {
  return {
    access_token: accessToken,
    refresh_token: "refresh-token",
    expires_in: 3600,
    token_type: "bearer",
    user,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve
  })
  return { promise, resolve }
}

function authClient(restoredSession: Session | null) {
  const sdk = {
    auth: {
      getSession: vi
        .fn()
        .mockResolvedValue({ data: { session: restoredSession }, error: null }),
      getUser: vi.fn().mockImplementation(async (token: string) => ({
        data: {
          user:
            token === restoredSession?.access_token
              ? restoredSession.user
              : null,
        },
        error: null,
      })),
      onAuthStateChange: () => ({
        data: { subscription: { unsubscribe: () => {} } },
      }),
      signOut: vi.fn().mockResolvedValue({ error: null }),
    },
  }
  const controller = createAuthController(sdk as unknown as SupabaseClient)
  const unsubscribe = controller.subscribe(() => {})
  return {
    controller,
    sdk,
    unsubscribe,
  }
}

afterEach(() => {
  window.history.replaceState(null, "", "/")
})

it("uses the refreshed token for the verified signed-in user", async () => {
  const auth = authClient(session(ada, "original-token"))
  try {
    await vi.waitFor(() =>
      expect(auth.controller.getSnapshot().status).toBe("signed-in")
    )
    auth.sdk.auth.getSession.mockResolvedValueOnce({
      data: { session: session(ada, "refreshed-token") },
      error: null,
    })

    await expect(auth.controller.getAccessToken()).resolves.toEqual({
      token: "refreshed-token",
      userId: "ada-id",
    })
  } finally {
    auth.unsubscribe()
  }
})

it("rejects a session token that belongs to another user", async () => {
  const auth = authClient(session(ada, "ada-token"))
  try {
    await vi.waitFor(() =>
      expect(auth.controller.getSnapshot().status).toBe("signed-in")
    )
    auth.sdk.auth.getSession.mockResolvedValueOnce({
      data: { session: session(bob, "bob-token") },
      error: null,
    })

    await expect(auth.controller.getAccessToken()).rejects.toThrow(
      "Sign in to continue."
    )
  } finally {
    auth.unsubscribe()
  }
})

it("locks backend access as soon as sign-out starts", async () => {
  const auth = authClient(session(ada, "ada-token"))
  await vi.waitFor(() =>
    expect(auth.controller.getSnapshot().status).toBe("signed-in")
  )
  let completeSignOut!: (value: { error: null }) => void
  const pendingSignOut = new Promise<{ error: null }>((resolve) => {
    completeSignOut = resolve
  })
  auth.sdk.auth.signOut.mockReturnValueOnce(pendingSignOut)
  const signingOut = auth.controller.signOut()

  try {
    expect(auth.controller.getSnapshot().status).toBe("loading")
    await expect(auth.controller.getAccessToken()).rejects.toThrow(
      "Sign in to continue."
    )
  } finally {
    completeSignOut({ error: null })
    await signingOut
    auth.unsubscribe()
  }
})

it("rejects an in-flight token refresh after sign-out", async () => {
  const auth = authClient(session(ada, "ada-token"))
  try {
    await vi.waitFor(() =>
      expect(auth.controller.getSnapshot().status).toBe("signed-in")
    )
    const pendingSession = deferred<{
      data: { session: Session }
      error: null
    }>()
    auth.sdk.auth.getSession.mockReturnValueOnce(pendingSession.promise)
    const credential = auth.controller.getAccessToken()
    await vi.waitFor(() =>
      expect(auth.sdk.auth.getSession).toHaveBeenCalledTimes(2)
    )

    await auth.controller.signOut()
    pendingSession.resolve({
      data: { session: session(ada, "late-ada-token") },
      error: null,
    })

    await expect(credential).rejects.toThrow("Sign in to continue.")
  } finally {
    auth.unsubscribe()
  }
})

it("rejects an old token when the same user signs in again", async () => {
  const auth = authClient(session(ada, "first-token"))
  try {
    await vi.waitFor(() =>
      expect(auth.controller.getSnapshot().status).toBe("signed-in")
    )
    const pendingSession = deferred<{
      data: { session: Session }
      error: null
    }>()
    auth.sdk.auth.getSession
      .mockReturnValueOnce(pendingSession.promise)
      .mockResolvedValueOnce({
        data: { session: session(ada, "second-token") },
        error: null,
      })
    const oldCredential = auth.controller.getAccessToken()
    await vi.waitFor(() =>
      expect(auth.sdk.auth.getSession).toHaveBeenCalledTimes(2)
    )

    await auth.controller.signOut()
    auth.sdk.auth.getUser.mockResolvedValueOnce({
      data: { user: ada },
      error: null,
    })
    await auth.controller.retry()
    expect(auth.controller.getSnapshot().status).toBe("signed-in")
    pendingSession.resolve({
      data: { session: session(ada, "first-token") },
      error: null,
    })

    await expect(oldCredential).rejects.toThrow("Sign in to continue.")
  } finally {
    auth.unsubscribe()
  }
})
