import assert from "node:assert/strict"
import { test } from "bun:test"
import type { User } from "@supabase/supabase-js"

import { createApiClient, ApiError } from "../src/lib/api"
import {
  apiUrl,
  parseBackendTargets,
  requiresDatabaseReadiness,
} from "../src/lib/backend-targets"
import {
  createServiceReadiness,
  ServiceReadinessError,
} from "../src/lib/service-readiness"

const hosted = {
  VITE_API_BASE_URL: "https://api.example.com",
  VITE_RUNTIME_WS_URL: "wss://api.example.com/api/runtime",
}

test("local development uses Vite proxies while hosted builds require explicit addresses", () => {
  assert.deepEqual(parseBackendTargets({}, false), {
    apiOrigin: null,
    runtimeWebSocketUrl: null,
  })
  assert.throws(() => parseBackendTargets({}, true), /Set VITE_API_BASE_URL/)
  assert.deepEqual(parseBackendTargets(hosted, true), {
    apiOrigin: "https://api.example.com",
    runtimeWebSocketUrl: "wss://api.example.com/api/runtime",
  })
  assert.equal(
    apiUrl("/api/sessions?limit=40", parseBackendTargets(hosted, true)),
    "https://api.example.com/api/sessions?limit=40"
  )
})

test("hosted configuration rejects unsafe, ambiguous, and mismatched transport targets", () => {
  for (const VITE_API_BASE_URL of [
    "http://api.example.com",
    "https://api.example.com/",
    "https://api.example.com/api",
    "https://user:secret@api.example.com",
    "https://api.example.com?token=secret",
    "https://api.example.com#fragment",
    "https://API.example.com",
    "https://localhost",
  ])
    assert.throws(() =>
      parseBackendTargets({ ...hosted, VITE_API_BASE_URL }, true)
    )
  for (const VITE_RUNTIME_WS_URL of [
    "https://api.example.com/api/runtime",
    "ws://api.example.com/api/runtime",
    "wss://frontend.example.com/api/runtime",
    "wss://api.example.com/api/runtime/",
    "wss://api.example.com/api/runtime?token=secret",
  ])
    assert.throws(() =>
      parseBackendTargets({ ...hosted, VITE_RUNTIME_WS_URL }, true)
    )
})

test("local explicit transport addresses remain available without weakening hosted checks", () => {
  const environment = {
    VITE_API_BASE_URL: "http://127.0.0.1:8000",
    VITE_RUNTIME_WS_URL: "ws://127.0.0.1:8000/api/runtime",
  }
  assert.equal(
    parseBackendTargets(environment, false).apiOrigin,
    environment.VITE_API_BASE_URL
  )
  assert.throws(() => parseBackendTargets(environment, true))
  assert.throws(() =>
    parseBackendTargets({ VITE_API_BASE_URL: hosted.VITE_API_BASE_URL }, false)
  )
})

test("only local development uses liveness instead of database readiness", () => {
  assert.equal(
    requiresDatabaseReadiness(parseBackendTargets({}, false), false),
    false
  )
  assert.equal(
    requiresDatabaseReadiness(
      parseBackendTargets(
        {
          VITE_API_BASE_URL: "http://127.0.0.1:8000",
          VITE_RUNTIME_WS_URL: "ws://127.0.0.1:8000/api/runtime",
        },
        false
      ),
      false
    ),
    false
  )
  assert.equal(
    requiresDatabaseReadiness(parseBackendTargets(hosted, true), true),
    true
  )
  assert.equal(
    requiresDatabaseReadiness(parseBackendTargets(hosted, false), false),
    true
  )
})

test("cold startup probes are unauthenticated and succeed before the protected operation", async () => {
  let probes = 0
  const paths: string[] = []
  const readiness = createServiceReadiness({
    hosted: true,
    url: () => "https://api.example.com/ready",
    retryDelaysMs: [1],
    fetch: async (input, init) => {
      paths.push(String(input))
      assert.equal(new Headers(init?.headers).has("Authorization"), false)
      assert.equal(init?.credentials, "omit")
      return ++probes < 3
        ? new Response("Service starting", { status: 503 })
        : Response.json({ status: "ready" })
    },
  })
  const signal = new AbortController().signal
  await readiness(signal)
  await readiness(signal)
  assert.deepEqual(paths, Array(3).fill("https://api.example.com/ready"))
})

test("local startup checks API liveness without waiting for hosted database readiness", async () => {
  const paths: string[] = []
  const readiness = createServiceReadiness({
    hosted: false,
    fetch: async (input) => {
      paths.push(String(input))
      return Response.json({ status: "ok" })
    },
  })
  await readiness(new AbortController().signal)
  assert.deepEqual(paths, ["/health"])
})

test("a reachable hosted API reports database readiness failure without cold-start retries", async () => {
  let requests = 0
  const readiness = createServiceReadiness({
    hosted: true,
    retryDelaysMs: [1],
    timeoutMs: 20,
    fetch: async () => {
      requests += 1
      return Response.json({ status: "unavailable" }, { status: 503 })
    },
  })
  await assert.rejects(
    readiness(new AbortController().signal),
    (error) =>
      error instanceof ServiceReadinessError &&
      error.code === "database_not_ready" &&
      error.message.includes("Supabase migrations")
  )
  assert.equal(requests, 1)
})

test("hosted startup cannot accept a liveness response as database readiness", async () => {
  const readiness = createServiceReadiness({
    hosted: true,
    fetch: async () => Response.json({ status: "ok" }),
  })
  await assert.rejects(
    readiness(new AbortController().signal),
    ServiceReadinessError
  )
})

test("startup retries are bounded even when fetch ignores cancellation", async () => {
  let probes = 0
  const readiness = createServiceReadiness({
    timeoutMs: 20,
    probeTimeoutMs: 5,
    retryDelaysMs: [1],
    fetch: () => {
      probes += 1
      return new Promise(() => {})
    },
  })
  await assert.rejects(
    readiness(new AbortController().signal),
    ServiceReadinessError
  )
  assert.ok(probes >= 1)
})

test("leaving a pending startup aborts its probe and prevents more requests", async () => {
  const controller = new AbortController()
  const reason = new Error("page left")
  let probeSignal: AbortSignal | null | undefined
  let requests = 0
  const readiness = createServiceReadiness({
    fetch: (_input, init) => {
      requests += 1
      probeSignal = init?.signal
      return new Promise(() => {})
    },
  })
  const pending = readiness(controller.signal)
  controller.abort(reason)
  await assert.rejects(pending, (error) => error === reason)
  assert.equal(probeSignal?.aborted, true)
  assert.equal(requests, 1)
})

test("permanent readiness errors fail immediately", async () => {
  let requests = 0
  const readiness = createServiceReadiness({
    fetch: async () => {
      requests += 1
      return new Response(null, { status: 404 })
    },
  })
  await assert.rejects(
    readiness(new AbortController().signal),
    ServiceReadinessError
  )
  assert.equal(requests, 1)
})

test("a frontend HTML rewrite cannot masquerade as a ready API", async () => {
  const readiness = createServiceReadiness({
    fetch: async () => new Response("<html>frontend</html>", { status: 200 }),
  })
  await assert.rejects(
    readiness(new AbortController().signal),
    ServiceReadinessError
  )
})

test("a protected mutation is sent once and a 503 does not expire the signed-in account", async () => {
  const credential = { token: "test-token", userId: "user-1", sessionEpoch: 1 }
  let expired = 0
  let mutations = 0
  let ready = false
  const client = createApiClient({
    resolveUrl: (path) => apiUrl(path, parseBackendTargets(hosted, true)),
    waitUntilReady: async () => {
      ready = true
    },
    getAuthController: () => ({
      getSnapshot: () => ({
        status: "signed-in",
        user: { id: "user-1" } as User,
        sessionEpoch: 1,
        error: null,
        pendingProvider: null,
      }),
      getAccessToken: async () => credential,
      isCredentialCurrent: () => true,
      expireSession: async () => {
        expired += 1
      },
    }),
    fetch: async (input, init) => {
      assert.equal(ready, true)
      assert.equal(input, "https://api.example.com/api/sessions")
      assert.equal(init?.method, "POST")
      mutations += 1
      return new Response("{}", { status: 503 })
    },
  })
  await assert.rejects(
    client.createSession(),
    (error) => error instanceof ApiError && error.status === 503
  )
  assert.equal(mutations, 1)
  assert.equal(expired, 0)
})

test("an account change during cold startup cannot send a stale mutation as the new account", async () => {
  let userId = "old-account"
  let epoch = 1
  let mutations = 0
  const client = createApiClient({
    waitUntilReady: async () => {
      userId = "new-account"
      epoch = 3
    },
    getAuthController: () => ({
      getSnapshot: () => ({
        status: "signed-in",
        user: { id: userId } as User,
        sessionEpoch: epoch,
        error: null,
        pendingProvider: null,
      }),
      getAccessToken: async () => ({
        token: "test",
        userId,
        sessionEpoch: epoch,
      }),
      isCredentialCurrent: () => true,
      expireSession: async () => {},
    }),
    fetch: async () => {
      mutations += 1
      return Response.json({ id: "wrong-account-session" })
    },
  })
  await assert.rejects(
    client.createSession(),
    (error) =>
      error instanceof ApiError && error.code === "authentication_required"
  )
  assert.equal(mutations, 0)
})
