import { expect, test } from "bun:test"

import { createApiClient } from "../apps/web/src/lib/api.ts"
import { AuthAccessError } from "../apps/web/src/lib/auth-controller.ts"

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function fixture() {
  let credential = { token: "test-token", userId: "account-a", sessionEpoch: 1 }
  let state = {
    status: "signed-in",
    user: { id: "account-a" },
    error: null,
    pendingProvider: null,
    sessionEpoch: 1,
  }
  const auth = {
    getSnapshot: () => state,
    getSessionEpoch: () => credential.sessionEpoch,
    getAccessToken: async () => credential,
    isCredentialCurrent: (candidate) =>
      state.status === "signed-in" &&
      state.user.id === candidate.userId &&
      credential.sessionEpoch === candidate.sessionEpoch,
    expireSession: async (candidate) => {
      if (
        !auth.isCredentialCurrent(candidate) ||
        candidate.token !== credential.token
      )
        return
      state = {
        ...state,
        status: "signed-out",
        user: null,
        error: "Your session expired. Please sign in again.",
      }
    },
  }
  const transport = { fetch: async () => Response.json([]) }
  return {
    auth,
    transport,
    api: createApiClient({
      getAuthController: () => auth,
      fetch: (...args) => transport.fetch(...args),
    }),
    get state() {
      return state
    },
    get credential() {
      return credential
    },
    setAccount(userId, sessionEpoch, token = "test-token") {
      credential = { token, userId, sessionEpoch }
      state = {
        status: "signed-in",
        user: { id: userId },
        error: null,
        pendingProvider: null,
        sessionEpoch,
      }
    },
  }
}

test("new chats never submit a legacy folder argument", async () => {
  const client = fixture()
  let sent
  client.transport.fetch = async (url, init) => {
    sent = { url, init }
    return Response.json({ id: "new-chat" })
  }
  const session = await client.api.createSession("/private/old-folder")
  expect(session.id).toBe("new-chat")
  expect(sent.url).toBe("/api/sessions")
  expect(sent.init.method).toBe("POST")
  expect(sent.init.body).toBeUndefined()
  expect(sent.init.headers.get("Authorization")).toBe("Bearer test-token")
})

test("chat model selection saves only the requested chat preference", async () => {
  const client = fixture()
  let sent
  client.transport.fetch = async (url, init) => {
    sent = { url, init }
    return Response.json({ id: "chat-a", model_id: "model-b" })
  }
  const session = await client.api.selectSessionModel("chat-a", "model-b")
  expect(session.model_id).toBe("model-b")
  expect(sent.url).toBe("/api/sessions/chat-a/model")
  expect(sent.init.method).toBe("PUT")
  expect(JSON.parse(sent.init.body)).toEqual({ model_id: "model-b" })
})

test("context preview posts the complete draft with its selected chat model and forwards cancellation", async () => {
  const client = fixture()
  const controller = new AbortController()
  const input = {
    session_id: "chat-a",
    model_id: "model-b",
    draft: "  Exact draft 🌳  ",
    budget_preset: "longer",
  }
  let sent
  client.transport.fetch = async (url, init) => {
    sent = { url, init }
    return Response.json({ model_id: "model-b" })
  }
  await client.api.previewContext(input, { signal: controller.signal })
  expect(sent.url).toBe("/api/context-preview")
  expect(sent.init.method).toBe("POST")
  expect(JSON.parse(sent.init.body)).toEqual(input)
  expect(sent.init.signal).toBeInstanceOf(AbortSignal)
})

test("an account change during token retrieval cannot send as the new account", async () => {
  const client = fixture()
  const token = deferred()
  client.auth.getAccessToken = () => token.promise
  let requests = 0
  client.transport.fetch = async () => {
    requests += 1
    return Response.json([])
  }
  const result = client.api.listSessions()
  client.setAccount("account-b", 2, "b-token")
  token.resolve(client.credential)
  await expect(result).rejects.toMatchObject({
    code: "authentication_required",
  })
  expect(requests).toBe(0)
  expect(client.state.user.id).toBe("account-b")
})

test("leaving and returning to the same account invalidates a pending token request", async () => {
  const client = fixture()
  const token = deferred()
  client.auth.getAccessToken = () => token.promise
  let requests = 0
  client.transport.fetch = async () => {
    requests += 1
    return Response.json([])
  }
  const result = client.api.listSessions()
  client.setAccount("account-b", 2)
  client.setAccount("account-a", 3)
  token.resolve(client.credential)
  await expect(result).rejects.toMatchObject({
    code: "authentication_required",
  })
  expect(requests).toBe(0)
})

test("a current rejected token expires the session through the guarded path", async () => {
  const client = fixture()
  client.transport.fetch = async () => new Response(null, { status: 401 })
  await expect(client.api.listSessions()).rejects.toMatchObject({
    code: "authentication_required",
  })
  expect(client.state.status).toBe("signed-out")
  expect(client.state.error).toBe("Your session expired. Please sign in again.")
})

test("a fetch outage has a safe retryable error and preserves the account", async () => {
  const client = fixture()
  client.transport.fetch = async () => {
    throw new TypeError("private fetch bearer-token-detail")
  }
  const error = await client.api.listSessions().catch((error) => error)
  expect(error).toMatchObject({ code: "service_unavailable", status: 0 })
  expect(error.message).not.toContain("bearer-token-detail")
  expect(client.state.status).toBe("signed-in")
})

test("a token outage has a safe retryable error and preserves the account", async () => {
  const client = fixture()
  client.auth.getAccessToken = async () => {
    throw new Error("private auth bearer-token-detail")
  }
  let requests = 0
  client.transport.fetch = async () => {
    requests += 1
    return Response.json([])
  }
  const error = await client.api.listSessions().catch((error) => error)
  expect(error).toMatchObject({ code: "service_unavailable", status: 0 })
  expect(error.message).not.toContain("bearer-token-detail")
  expect(requests).toBe(0)
  expect(client.state.status).toBe("signed-in")
})

test("a response from an earlier sign-in cannot enter the same user's new session", async () => {
  const client = fixture()
  const response = deferred()
  const sent = deferred()
  client.transport.fetch = () => {
    sent.resolve()
    return response.promise
  }
  const result = client.api.listSessions()
  await sent.promise
  client.setAccount("account-b", 2)
  client.setAccount("account-a", 3)
  response.resolve(Response.json([{ id: "old-account-chat" }]))
  await expect(result).rejects.toMatchObject({
    code: "authentication_required",
  })
  expect(client.state.status).toBe("signed-in")
})

test("account changes while decoding a response cannot expose its old data", async () => {
  const client = fixture()
  const body = deferred()
  const decoding = deferred()
  client.transport.fetch = async () => ({
    ok: true,
    status: 200,
    json: () => {
      decoding.resolve()
      return body.promise
    },
  })
  const result = client.api.listSessions()
  await decoding.promise
  client.setAccount("account-b", 2)
  body.resolve([{ id: "old-account-chat" }])
  await expect(result).rejects.toMatchObject({
    code: "authentication_required",
  })
})

test("a successful response stays usable during ordinary token refresh", async () => {
  const client = fixture()
  const response = deferred()
  const sent = deferred()
  client.transport.fetch = () => {
    sent.resolve()
    return response.promise
  }
  const result = client.api.listSessions()
  await sent.promise
  client.setAccount("account-a", 1, "refreshed-token")
  response.resolve(Response.json([{ id: "same-session-chat" }]))
  expect(await result).toEqual([{ id: "same-session-chat" }])
  expect(client.state.status).toBe("signed-in")
})

test("an old unauthorized response cannot expire a refreshed token", async () => {
  const client = fixture()
  const response = deferred()
  const sent = deferred()
  client.transport.fetch = () => {
    sent.resolve()
    return response.promise
  }
  const result = client.api.listSessions()
  await sent.promise
  client.setAccount("account-a", 1, "refreshed-token")
  response.resolve(new Response(null, { status: 401 }))
  await expect(result).rejects.toMatchObject({
    code: "authentication_required",
  })
  expect(client.state.status).toBe("signed-in")
  expect(client.credential.token).toBe("refreshed-token")
})

test("ordinary refresh preserves an in-flight service error", async () => {
  const client = fixture()
  const response = deferred()
  const sent = deferred()
  client.transport.fetch = () => {
    sent.resolve()
    return response.promise
  }
  const result = client.api.listSessions()
  await sent.promise
  client.setAccount("account-a", 1, "refreshed-token")
  response.resolve(
    Response.json(
      {
        error: {
          code: "provider_not_available",
          message: "The selected provider is not available.",
        },
      },
      { status: 503 }
    )
  )
  await expect(result).rejects.toMatchObject({
    code: "provider_not_available",
    status: 503,
  })
  expect(client.state.status).toBe("signed-in")
})

test("an auth outage during a rejected-token check cannot expire the account", async () => {
  const client = fixture()
  client.transport.fetch = async () => new Response(null, { status: 401 })
  let tokens = 0
  client.auth.getAccessToken = async () => {
    tokens += 1
    if (tokens > 1) throw new Error("private token-check detail")
    return client.credential
  }
  await expect(client.api.listSessions()).rejects.toMatchObject({
    code: "service_unavailable",
  })
  expect(client.state.status).toBe("signed-in")
})

test("a timed-out token retrieval cannot send a late request", async () => {
  const client = fixture()
  const token = deferred()
  client.auth.getAccessToken = () => token.promise
  let requests = 0
  client.transport.fetch = async () => {
    requests += 1
    return Response.json([])
  }
  await expect(client.api.getProfile({ timeoutMs: 5 })).rejects.toMatchObject({
    code: "request_timeout",
  })
  token.resolve(client.credential)
  await Promise.resolve()
  expect(requests).toBe(0)
  expect(client.state.status).toBe("signed-in")
})

test("a timed-out request aborts fetch and ignores a late unauthorized response", async () => {
  const client = fixture()
  const response = deferred()
  let signal
  client.transport.fetch = (_url, init) => {
    signal = init.signal
    return response.promise
  }
  await expect(client.api.getProfile({ timeoutMs: 5 })).rejects.toMatchObject({
    code: "request_timeout",
  })
  expect(signal.aborted).toBe(true)
  client.setAccount("account-b", 2, "b-token")
  response.resolve(new Response(null, { status: 401 }))
  await Promise.resolve()
  expect(client.state.status).toBe("signed-in")
  expect(client.state.user.id).toBe("account-b")
})

test("invalid success JSON has a safe service error", async () => {
  const client = fixture()
  client.transport.fetch = async () =>
    new Response("private-invalid-json", { status: 200 })
  const error = await client.api.listSessions().catch((error) => error)
  expect(error).toMatchObject({ code: "service_unavailable" })
  expect(error.message).not.toContain("private-invalid-json")
  expect(client.state.status).toBe("signed-in")
})

test("controller resolution failures use the safe service error", async () => {
  const client = createApiClient({
    getAuthController: () => {
      throw new Error("private-auth-configuration")
    },
  })
  const error = await client.listSessions().catch((error) => error)
  expect(error).toMatchObject({ code: "service_unavailable", status: 0 })
  expect(error.message).not.toContain("private-auth-configuration")
})

test.each([
  null,
  { error: { message: ["private-upstream-data"] } },
  { detail: ["private-upstream-data"] },
])(
  "malformed service error payloads keep the safe request failure",
  async (payload) => {
    const client = fixture()
    client.transport.fetch = async () => Response.json(payload, { status: 503 })
    const error = await client.api.listSessions().catch((error) => error)
    expect(error).toMatchObject({
      code: "request_failed",
      status: 503,
      message: "Trellis could not complete that request.",
    })
    expect(client.state.status).toBe("signed-in")
  }
)

test("typed definitive auth failures keep the authentication-required contract", async () => {
  const client = fixture()
  client.auth.getAccessToken = async () => {
    throw new AuthAccessError("reauth_required", "private-auth-failure")
  }
  const error = await client.api.listSessions().catch((error) => error)
  expect(error).toMatchObject({ code: "authentication_required", status: 401 })
  expect(error.message).not.toContain("private-auth-failure")
})

test("typed transient auth failures are retryable and preserve the account", async () => {
  const client = fixture()
  client.auth.getAccessToken = async () => {
    throw new AuthAccessError("auth_unavailable", "private-auth-outage")
  }
  const error = await client.api.listSessions().catch((error) => error)
  expect(error).toMatchObject({ code: "service_unavailable", status: 0 })
  expect(error.message).not.toContain("private-auth-outage")
  expect(client.state.status).toBe("signed-in")
})

test("caller cancellation prevents a late token from sending a request", async () => {
  const client = fixture()
  const token = deferred()
  const controller = new AbortController()
  client.auth.getAccessToken = () => token.promise
  let requests = 0
  client.transport.fetch = async () => {
    requests += 1
    return Response.json([])
  }
  const result = client.api.listSessions({ signal: controller.signal })
  controller.abort()
  await expect(result).rejects.toMatchObject({ name: "AbortError" })
  token.resolve(client.credential)
  await Promise.resolve()
  expect(requests).toBe(0)
  expect(client.state.status).toBe("signed-in")
})
