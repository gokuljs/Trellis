import assert from "node:assert/strict"
import { afterEach, test } from "bun:test"
import * as Sentry from "@sentry/react"
import { CancelledError } from "@tanstack/react-query"

import { parseTelemetryConfig } from "../src/lib/telemetry-policy"
import {
  createTelemetryOptions,
  initializeTelemetry,
  reportFailure,
  telemetryBrowserTracingOptions,
  telemetryRootOptions,
} from "../src/lib/telemetry"

const origin = "https://trellis.test"
type RecordedEnvelope = Parameters<
  ReturnType<NonNullable<Sentry.BrowserOptions["transport"]>>["send"]
>[0]
const config = parseTelemetryConfig({
  MODE: "production",
  PROD: true,
  VITE_SENTRY_DSN: "https://public@o123.ingest.sentry.io/456",
  VITE_SENTRY_RELEASE: "web@1234567",
  VITE_SENTRY_TRACES_SAMPLE_RATE: "1",
  origin,
})

afterEach(async () => {
  await Sentry.close(1000)
  Sentry.setCurrentClient(undefined)
})

function recordingClient(tracesSampleRate = config.tracesSampleRate) {
  const envelopes: RecordedEnvelope[] = []
  const client = Sentry.init({
    ...createTelemetryOptions({ ...config, tracesSampleRate }),
    transport: () => ({
      send: (envelope) => {
        envelopes.push(envelope)
        return Promise.resolve({ statusCode: 200 })
      },
      flush: () => Promise.resolve(true),
    }),
  })
  assert.ok(client)
  return { client, envelopes }
}

function errorEvents(envelopes: RecordedEnvelope[]) {
  return envelopes.flatMap((envelope) =>
    envelope[1].flatMap(([header, payload]) =>
      header.type === "event" ? [payload as Sentry.ErrorEvent] : []
    )
  )
}

test("disabled configuration does not initialize and enabled initialization is idempotent", () => {
  initializeTelemetry({ ...config, enabled: false })
  assert.equal(Sentry.getClient(), undefined)
  initializeTelemetry(config)
  const client = Sentry.getClient()
  assert.ok(client)
  initializeTelemetry(config)
  assert.equal(Sentry.getClient(), client)
})

test("React root hooks capture once per failure and mark recoverable errors as warnings", async () => {
  assert.equal(telemetryRootOptions(), undefined)
  const { client, envelopes } = recordingClient()
  const handlers = telemetryRootOptions()!
  const info = {
    componentStack: `\n    at Fixture (${origin}/src/Fixture.tsx:7:4)`,
  }
  handlers.onCaughtError!(new Error("private caught content"), {
    ...info,
    errorBoundary: null,
  })
  handlers.onUncaughtError!(new TypeError("private uncaught content"), info)
  handlers.onRecoverableError!(new Error("private hydration content"), info)
  await client.flush(1000)
  const events = errorEvents(envelopes)
  assert.equal(events.length, 3)
  assert.deepEqual(
    events.map((event) => event.level),
    ["error", "error", "warning"]
  )
  assert.equal(JSON.stringify(envelopes).includes("private"), false)
  assert.equal(events[0]?.exception?.values?.at(-1)?.mechanism?.handled, true)
  assert.equal(events[1]?.exception?.values?.at(-1)?.mechanism?.handled, false)
})

test("browser tracing names paths early and instruments only Trellis API requests", () => {
  const options = telemetryBrowserTracingOptions(config)
  const span = options.beforeStartSpan!({
    name: "/api/sessions/private?draft=private#oauth",
  })
  assert.equal(span.name, "/api/sessions/:id")
  for (const [url, expected] of [
    ["/api/sessions", true],
    [`${origin}/api/settings`, true],
    ["https://supabase.test/api/profile", false],
    ["https://trellis.test.evil.test/api/settings", false],
    ["/apiary", false],
    ["/assets/index.js", false],
  ] as const)
    assert.equal(options.shouldCreateSpanForRequest!(url), expected, url)
})

test("outgoing SDK envelopes retain source frames while removing hostile scope and linked errors", async () => {
  const { client, envelopes } = recordingClient()
  const secret = "private-draft-token-email@example.test"
  const cause = new Error(`linked ${secret}`)
  cause.stack = `Error: ${secret}\n    at linked (${origin}/assets/index-12345678.js?access_token=${secret}#${secret}:5:10)`
  const failure = new TypeError(`message ${secret}`, { cause })
  failure.stack = `TypeError: ${secret}\n    at render (${origin}/assets/index-12345678.js?access_token=${secret}#${secret}:12:8)`
  Sentry.withScope((scope) => {
    scope.setUser({ id: secret, email: secret })
    scope.setTag("raw-private", secret)
    scope.setExtra("draft", secret)
    scope.setFingerprint([secret])
    scope.setContext("rpc-frame", { params: secret })
    scope.addAttachment({ filename: secret, data: secret })
    scope.addBreadcrumb({ message: secret, data: { secret } })
    reportFailure(failure, "context.preview")
  })
  await client.flush(1000)
  assert.equal(errorEvents(envelopes).length, 1)
  const encoded = JSON.stringify(envelopes)
  assert.equal(encoded.includes(secret), false, encoded)
  const event = errorEvents(envelopes)[0]!
  assert.equal(event.release, "web@1234567")
  assert.equal(event.tags?.["telemetry.operation"], "context.preview")
  assert.equal(event.exception?.values?.length, 2)
  const frame = event.exception?.values?.at(-1)?.stacktrace?.frames?.at(-1)
  assert.equal(frame?.filename, `${origin}/assets/index-12345678.js`)
  assert.equal(frame?.lineno, 12)
  assert.equal(frame?.colno, 8)
  assert.equal(event.user, undefined)
  assert.equal(event.extra, undefined)
  assert.equal(event.fingerprint, undefined)
  assert.deepEqual(client.getOptions().dataCollection?.httpBodies, [])
  const integrationNames = client
    .getOptions()
    .integrations.map((integration) => integration.name)
  for (const excluded of [
    "Console",
    "Breadcrumbs",
    "ConversationId",
    "Dedupe",
    "BrowserSession",
    "HttpContext",
    "CultureContext",
    "Replay",
    "BrowserProfiling",
    "Feedback",
  ]) {
    assert.equal(integrationNames.includes(excluded), false, excluded)
  }
})

test("failure identity suppresses repeated reports without merging distinct errors", async () => {
  const { client, envelopes } = recordingClient()
  const failure = new Error("private text")
  reportFailure(failure, "sessions.open")
  reportFailure(failure, "history.load")
  const other = new Error("private text")
  other.stack = failure.stack
  reportFailure(other, "sessions.open")
  reportFailure(new CancelledError(), "sessions.list")
  reportFailure(
    Object.assign(new Error("private"), { name: "ApiError", status: 401 }),
    "sessions.list"
  )
  await client.flush(1000)
  assert.equal(errorEvents(envelopes).length, 2)
})

test("automatic capture removes hostile exception types, SDK fields, and sampling metadata", async () => {
  const { client, envelopes } = recordingClient()
  const secret = "privatePromptError"
  Sentry.withScope((scope) => {
    scope.setSDKProcessingMetadata({
      dynamicSamplingContext: { trace_id: "a".repeat(32), transaction: secret },
    })
    Sentry.captureEvent({
      message: secret,
      logentry: { message: secret, params: [secret] },
      logger: secret,
      server_name: secret,
      release: secret,
      environment: secret,
      modules: { [secret]: secret },
      fingerprint: [secret],
      request: {
        url: `${origin}/oauth/callback?code=${secret}#access_token=${secret}`,
        headers: { authorization: secret },
        cookies: { token: secret },
        data: secret,
      },
      exception: {
        values: [
          {
            type: secret,
            value: secret,
            mechanism: {
              type: `auto.browser.${secret}`,
              handled: false,
              data: { secret },
            },
            stacktrace: {
              frames: [
                {
                  filename: `${origin}/assets/index-12345678.js?token=${secret}`,
                  function: secret,
                  module: secret,
                  context_line: secret,
                  vars: { secret },
                  lineno: 7,
                  colno: 4,
                },
              ],
            },
          },
        ],
      },
      sdk: {
        name: secret,
        version: secret,
        integrations: [secret],
        packages: [{ name: secret, version: secret }],
      },
    })
  })
  await client.flush(1000)
  assert.equal(errorEvents(envelopes).length, 1)
  const encoded = JSON.stringify(envelopes)
  assert.equal(encoded.includes(secret), false, encoded)
  assert.equal(errorEvents(envelopes)[0]?.exception?.values?.[0]?.type, "Error")
  assert.equal(errorEvents(envelopes)[0]?.request?.url, "/:path")
})

test("actual streamed span envelopes remove content from names, attributes, links, and headers", async () => {
  const { client, envelopes } = recordingClient()
  const secret = "private-prompt-and-oauth-token"
  Sentry.startSpan(
    {
      name: `GET ${origin}/api/sessions/${secret}?access_token=${secret}#${secret}`,
      op: "http.client",
      attributes: {
        "http.request.method": "GET",
        "url.full": `${origin}/api/sessions/${secret}?access_token=${secret}`,
        "http.response.status_code": 503,
        "gen_ai.input.messages": secret,
        secret,
      },
    },
    (span) => {
      span.addLink({
        context: { traceId: "a".repeat(32), spanId: "b".repeat(16) },
        attributes: { secret },
      })
    }
  )
  await client.flush(1000)
  const spans = envelopes.flatMap((envelope) =>
    envelope[1].filter(([header]) => header.type === "span")
  )
  assert.equal(spans.length, 1)
  const encoded = JSON.stringify(envelopes)
  assert.equal(encoded.includes(secret), false, encoded)
  assert.ok(encoded.includes("GET /api/sessions/:id"), encoded)
  assert.ok(encoded.includes("503"), encoded)
})

test("inherited sampling data is sanitized before sampled and unsampled request headers", async () => {
  const secret = "private-incoming-user-and-transaction"
  for (const rate of [1, 0]) {
    const { client, envelopes } = recordingClient(rate)
    Sentry.withScope((scope) => {
      const dsc = {
        trace_id: "a".repeat(32),
        public_key: "public",
        transaction: secret,
        user_id: secret,
        replay_id: secret,
        sample_rate: "1",
      }
      scope.setPropagationContext({
        traceId: "a".repeat(32),
        sampleRand: 0.5,
        dsc,
      })
      const span = Sentry.startInactiveSpan({ name: secret, op: "http.client" })
      for (const headers of [
        Sentry.getTraceData({ span }),
        Sentry.getTraceData(),
      ]) {
        assert.equal(
          JSON.stringify(headers).includes(secret),
          false,
          JSON.stringify(headers)
        )
        assert.ok(headers["sentry-trace"]?.startsWith("a".repeat(32)))
      }
      span.end()
      reportFailure(new Error(secret), "sessions.list")
    })
    await client.flush(1000)
    const encoded = JSON.stringify(envelopes)
    assert.equal(encoded.includes(secret), false, encoded)
    assert.equal(errorEvents(envelopes).length, 1)
    await client.close(1000)
    Sentry.setCurrentClient(undefined)
  }
})
