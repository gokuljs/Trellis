import { afterEach, beforeEach, expect, spyOn, test } from "bun:test"

import { createAuthController } from "../apps/web/src/lib/auth-controller.ts"

const expiredMessage = "Your session expired. Please sign in again"
const originalWindow = globalThis.window
const subscriptions = []

function session(userId = "account-a", token = "first-token") {
  return {
    access_token: token,
    refresh_token: "refresh-token",
    expires_in: 3600,
    token_type: "bearer",
    user: {
      id: userId,
      aud: "authenticated",
      email: `${userId}@example.com`,
      app_metadata: { provider: "google" },
      user_metadata: {},
      created_at: "2026-10-10T00:00:00Z",
    },
  }
}

function deferred() {
  let resolve
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve }
}

async function settle() {
  for (let index = 0; index < 12; index++) await Promise.resolve()
}

async function restoredAuth(
  initial = session(),
  configure = () => {},
  expectedStatus = initial ? "signed-in" : "signed-out"
) {
  let stored = initial
  let callback
  const users = new Map(initial ? [[initial.access_token, initial.user]] : [])
  const sdk = {
    auth: {
      getSession: async () => ({ data: { session: stored }, error: null }),
      getUser: async (token) => ({
        data: { user: users.get(token) ?? null },
        error: null,
      }),
      onAuthStateChange: (listener) => {
        callback = listener
        return { data: { subscription: { unsubscribe: () => {} } } }
      },
      signOut: async () => {
        stored = null
        callback("SIGNED_OUT", null)
        return { error: null }
      },
    },
  }
  configure(sdk)
  const controller = createAuthController(sdk, () => {})
  subscriptions.push(controller.subscribe(() => {}))
  await settle()
  expect(controller.getSnapshot().status).toBe(expectedStatus)
  return {
    controller,
    sdk,
    emit: (event, nextSession) => {
      stored = nextSession
      if (nextSession) users.set(nextSession.access_token, nextSession.user)
      callback(event, nextSession)
    },
  }
}

beforeEach(() => {
  const location = {
    href: "https://trellis.test/",
    origin: "https://trellis.test",
  }
  globalThis.window = Object.assign(new EventTarget(), {
    location,
    history: {
      state: null,
      replaceState: (_state, _title, url) => {
        location.href = new URL(url, location.href).href
      },
    },
  })
})

afterEach(() => {
  for (const unsubscribe of subscriptions.splice(0)) unsubscribe()
  globalThis.window = originalWindow
})

test("credentials include the identity epoch captured with a refreshed token", async () => {
  const { controller, sdk } = await restoredAuth()
  sdk.auth.getSession = async () => ({
    data: { session: session("account-a", "refreshed-token") },
    error: null,
  })
  const credential = await controller.getAccessToken()
  expect(credential).toEqual({
    token: "refreshed-token",
    userId: "account-a",
    sessionEpoch: controller.getSessionEpoch(),
  })
})

test("a missing session locks account access before pending cleanup finishes", async () => {
  const { controller, sdk } = await restoredAuth()
  const cleanup = deferred()
  sdk.auth.signOut = () => cleanup.promise
  sdk.auth.getSession = async () => ({ data: { session: null }, error: null })
  const pending = controller.getAccessToken().catch((error) => error)
  await settle()
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-out",
    user: null,
    error: expiredMessage,
  })
  cleanup.resolve({ error: new Error("cleanup failed") })
  const rejected = await pending
  expect(rejected.code).toBe("reauth_required")
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-out",
    user: null,
    error: expiredMessage,
  })
})

for (const error of [
  { name: "AuthSessionMissingError", status: 400 },
  { code: "refresh_token_already_used", status: 400 },
  { code: "bad_jwt", status: 401 },
]) {
  test(`invalid SDK session ${error.name ?? error.code} expires the account without exposing details`, async () => {
    const { controller, sdk } = await restoredAuth()
    sdk.auth.getSession = async () => ({
      data: { session: null },
      error: { ...error, message: "private SDK details" },
    })
    const rejected = await controller.getAccessToken().catch((caught) => caught)
    expect(rejected.code).toBe("reauth_required")
    expect(rejected.message).toBe(expiredMessage)
    expect(controller.getSnapshot()).toMatchObject({
      status: "signed-out",
      user: null,
      error: expiredMessage,
    })
  })
}

test("an auth outage keeps the current signed-in identity available for retry", async () => {
  const { controller, sdk } = await restoredAuth()
  const epoch = controller.getSessionEpoch()
  sdk.auth.getSession = async () => ({
    data: { session: null },
    error: { status: 503, message: "private upstream failure" },
  })
  const rejected = await controller.getAccessToken().catch((error) => error)
  expect(rejected.code).toBe("auth_unavailable")
  expect(rejected.message).not.toContain("private")
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-in",
    user: { id: "account-a" },
  })
  expect(controller.getSessionEpoch()).toBe(epoch)
})

test("a bounded auth timeout permits retry without expiring the identity", async () => {
  const { controller, sdk } = await restoredAuth()
  sdk.auth.getSession = () => new Promise(() => {})
  const originalSetTimeout = globalThis.setTimeout
  const timer = spyOn(globalThis, "setTimeout").mockImplementation(
    (callback, duration, ...args) =>
      originalSetTimeout(callback, duration === 15000 ? 0 : duration, ...args)
  )
  try {
    const rejected = await controller.getAccessToken().catch((error) => error)
    expect(rejected.code).toBe("auth_unavailable")
    expect(controller.getSnapshot().status).toBe("signed-in")
  } finally {
    timer.mockRestore()
  }
})

test("failed expiry cleanup cannot restore a cached rejected session on retry", async () => {
  const { controller, sdk, emit } = await restoredAuth()
  const credential = await controller.getAccessToken()
  sdk.auth.signOut = async () => ({ error: new Error("cleanup failed") })
  await controller.expireSession(credential)
  await controller.retry()
  emit("SIGNED_IN", session())
  await settle()
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-out",
    user: null,
    error: expiredMessage,
  })
})

test("failed expiry cleanup blocks OAuth until cleanup succeeds", async () => {
  const { controller, sdk } = await restoredAuth()
  const credential = await controller.getAccessToken()
  let redirected = false
  sdk.auth.signInWithOAuth = async () => {
    redirected = true
    return { data: { url: "https://provider.test/" }, error: null }
  }
  sdk.auth.signOut = async () => ({ error: new Error("cleanup failed") })
  await controller.expireSession(credential)
  await controller.signIn("google")
  expect(redirected).toBe(false)
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-out",
    user: null,
    error: expiredMessage,
  })
})

test("an invalid-session sign-out failure cannot restore the signed-in account", async () => {
  const { controller, sdk } = await restoredAuth()
  sdk.auth.signOut = async () => ({ error: { code: "bad_jwt", status: 401 } })
  await controller.signOut()
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-out",
    user: null,
    error: expiredMessage,
  })
})

test("an expiry cleanup timeout remains locked and rejects late sign-in events", async () => {
  const { controller, sdk, emit } = await restoredAuth()
  const credential = await controller.getAccessToken()
  sdk.auth.signOut = () => new Promise(() => {})
  const originalSetTimeout = globalThis.setTimeout
  const timer = spyOn(globalThis, "setTimeout").mockImplementation(
    (callback, duration, ...args) =>
      originalSetTimeout(callback, duration === 15000 ? 0 : duration, ...args)
  )
  try {
    await controller.expireSession(credential)
    emit("SIGNED_IN", session())
    await settle()
    expect(controller.getSnapshot()).toMatchObject({
      status: "signed-out",
      user: null,
      error: expiredMessage,
    })
  } finally {
    timer.mockRestore()
  }
})

test("ordinary token refresh preserves the epoch and protects the fresh token from stale expiry", async () => {
  const { controller, emit } = await restoredAuth()
  const original = await controller.getAccessToken()
  emit("TOKEN_REFRESHED", session("account-a", "fresh-token"))
  await settle()
  expect(controller.getSnapshot().sessionEpoch).toBe(original.sessionEpoch)
  expect(controller.isCredentialCurrent(original)).toBe(true)
  await controller.expireSession(original)
  expect(controller.getSnapshot().status).toBe("signed-in")
  expect((await controller.getAccessToken()).token).toBe("fresh-token")
})

test("same-account new sign-in changes the identity epoch and ignores old expiry", async () => {
  const { controller, emit } = await restoredAuth()
  const original = await controller.getAccessToken()
  emit("SIGNED_IN", session("account-a", "new-sign-in-token"))
  await settle()
  expect(controller.getSnapshot().sessionEpoch).not.toBe(original.sessionEpoch)
  expect(controller.isCredentialCurrent(original)).toBe(false)
  await controller.expireSession(original)
  expect(controller.getSnapshot().status).toBe("signed-in")
})

test("a token read before a new same-account sign-in cannot hide the identity change", async () => {
  const { controller, sdk, emit } = await restoredAuth()
  const original = await controller.getAccessToken()
  const nextSession = session("account-a", "new-sign-in-token")
  sdk.auth.getSession = async () => ({
    data: { session: nextSession },
    error: null,
  })
  expect((await controller.getAccessToken()).token).toBe(
    nextSession.access_token
  )
  emit("SIGNED_IN", nextSession)
  await settle()
  expect(controller.getSnapshot().sessionEpoch).not.toBe(original.sessionEpoch)
  expect(controller.isCredentialCurrent(original)).toBe(false)
})

for (const rejectedOperation of ["getSession", "getUser"]) {
  test(`startup ${rejectedOperation} invalid-session failure stays expired when cleanup fails`, async () => {
    const { controller, sdk, emit } = await restoredAuth(
      session(),
      (client) => {
        client.auth[rejectedOperation] = async () => ({
          data:
            rejectedOperation === "getSession"
              ? { session: null }
              : { user: null },
          error: { code: "refresh_token_already_used", status: 400 },
        })
        client.auth.signOut = async () => ({
          error: new Error("cleanup failed"),
        })
      },
      "signed-out"
    )
    expect(controller.getSnapshot()).toMatchObject({
      user: null,
      error: expiredMessage,
    })
    sdk.auth.getSession = async () => ({
      data: { session: session() },
      error: null,
    })
    sdk.auth.getUser = async () => ({
      data: { user: session().user },
      error: null,
    })
    await controller.retry()
    emit("SIGNED_IN", session())
    await settle()
    expect(controller.getSnapshot()).toMatchObject({
      status: "signed-out",
      user: null,
      error: expiredMessage,
    })
  })
}

for (const verifiedUser of [null, session("wrong-account").user]) {
  test(`startup ${verifiedUser ? "mismatched" : "missing"} verified user expires the cached session`, async () => {
    const { controller } = await restoredAuth(
      session(),
      (client) => {
        client.auth.getUser = async () => ({
          data: { user: verifiedUser },
          error: null,
        })
        client.auth.signOut = async () => ({
          error: new Error("cleanup failed"),
        })
      },
      "signed-out"
    )
    expect(controller.getSnapshot()).toMatchObject({
      user: null,
      error: expiredMessage,
    })
    await controller.retry()
    expect(controller.getSnapshot()).toMatchObject({
      status: "signed-out",
      user: null,
      error: expiredMessage,
    })
  })
}

test("a startup auth outage allows retry to restore the valid cached session", async () => {
  const { controller, sdk } = await restoredAuth(
    session(),
    (client) => {
      client.auth.getUser = async () => ({
        data: { user: null },
        error: { status: 503, message: "private upstream error" },
      })
    },
    "error"
  )
  expect(controller.getSnapshot().error).not.toContain("private")
  sdk.auth.getUser = async () => ({
    data: { user: session().user },
    error: null,
  })
  await controller.retry()
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-in",
    user: { id: "account-a" },
    error: null,
  })
})

test("a successful PKCE callback is consumed once and removed from the browser URL", async () => {
  window.location.href =
    "https://trellis.test/?code=callback-code&sb_flow_id=flow-id&keep=value#code=fragment-code"
  const exchanges = []
  const { controller } = await restoredAuth(session(), (client) => {
    client.auth.exchangeCodeForSession = async (...args) => {
      exchanges.push(args)
      return { data: { session: session() }, error: null }
    }
    client.auth.getSession = async () => {
      throw new Error("PKCE restoration must use the exchanged session")
    }
  })
  expect(exchanges).toEqual([["callback-code", { flowId: "flow-id" }]])
  expect(window.location.href).toBe("https://trellis.test/?keep=value")
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-in",
    user: { id: "account-a" },
  })
})

test("successful expiry cleanup permits a fresh sign-in after an earlier cleanup failure", async () => {
  const { controller, sdk, emit } = await restoredAuth()
  const original = await controller.getAccessToken()
  sdk.auth.signOut = async () => ({ error: new Error("cleanup failed") })
  await controller.expireSession(original)
  sdk.auth.signOut = async () => ({ error: null })
  let redirected = false
  sdk.auth.signInWithOAuth = async () => {
    redirected = true
    return { data: { url: "https://provider.test/" }, error: null }
  }
  await controller.signIn("google")
  expect(redirected).toBe(true)
  emit("SIGNED_IN", session("account-a", "fresh-sign-in-token"))
  await settle()
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-in",
    user: { id: "account-a" },
    error: null,
  })
  expect(controller.isCredentialCurrent(original)).toBe(false)
})

test("an in-flight credential cannot cross account A to B to A", async () => {
  const { controller, sdk, emit } = await restoredAuth()
  const original = await controller.getAccessToken()
  const pendingSession = deferred()
  sdk.auth.getSession = () => pendingSession.promise
  const pendingCredential = controller.getAccessToken().catch((error) => error)
  await settle()
  emit("SIGNED_IN", session("account-b", "b-token"))
  await settle()
  emit("SIGNED_IN", session("account-a", "second-a-token"))
  await settle()
  pendingSession.resolve({ data: { session: session() }, error: null })
  expect((await pendingCredential).code).toBe("reauth_required")
  await controller.expireSession(original)
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-in",
    user: { id: "account-a" },
  })
})

test("a validated refreshed token is current before subscribers observe signed-in state", async () => {
  const { controller, emit } = await restoredAuth()
  const original = await controller.getAccessToken()
  let observed = false
  subscriptions.push(
    controller.subscribe(() => {
      if (observed || controller.getSnapshot().status !== "signed-in") return
      observed = true
      void controller.expireSession({ ...original, token: "fresh-token" })
    })
  )
  emit("TOKEN_REFRESHED", session("account-a", "fresh-token"))
  await settle()
  expect(observed).toBe(true)
  expect(controller.getSnapshot()).toMatchObject({
    status: "signed-out",
    user: null,
    error: expiredMessage,
  })
})
