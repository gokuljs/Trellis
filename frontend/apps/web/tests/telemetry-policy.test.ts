import assert from "node:assert/strict"
import { test } from "bun:test"
import { CancelledError } from "@tanstack/react-query"

import {
  classifyTelemetryFailure,
  isSameOriginApiRequest,
  normalizeTelemetryPath,
  parseTelemetryConfig,
  sanitizeTelemetryEvent,
  sanitizeTelemetrySpan,
  telemetryTraceTargets,
} from "../src/lib/telemetry-policy"

const dsn = "https://public@o123.ingest.sentry.io/456"
const origin = "https://trellis.test"
const config = parseTelemetryConfig({
  MODE: "production",
  PROD: true,
  VITE_SENTRY_DSN: dsn,
  origin,
})

test("activation requires valid configuration and production or explicit opt in", () => {
  for (const [env, enabled] of [
    [{}, false],
    [{ VITE_SENTRY_DSN: "garbage" }, false],
    [{ VITE_SENTRY_DSN: "https://public@example.test/not-a-project" }, false],
    [{ MODE: "development", PROD: false }, false],
    [{ MODE: "preview" }, false],
    [{ VITE_SENTRY_ENVIRONMENT: "staging" }, false],
    [{ VITE_SENTRY_ENABLED: "true", MODE: "development", PROD: false }, true],
    [{ VITE_SENTRY_ENABLED: "false" }, false],
    [{ VITE_SENTRY_ENABLED: "" }, true],
    [{ VITE_SENTRY_ENABLED: "  " }, true],
    [{ VITE_SENTRY_ENABLED: "yes" }, false],
  ] as const) {
    assert.equal(
      parseTelemetryConfig({
        MODE: "production",
        PROD: true,
        ...(Object.keys(env).length ? { VITE_SENTRY_DSN: dsn } : {}),
        ...env,
      }).enabled,
      enabled,
      JSON.stringify(env)
    )
  }
  assert.equal(config.enabled, true)
  assert.equal(config.environment, "production")
  assert.equal(config.tracesSampleRate, 0.1)
  assert.equal(
    parseTelemetryConfig({
      VITE_SENTRY_ENVIRONMENT: " staging ",
      VITE_SENTRY_RELEASE: " abc123 ",
    }).environment,
    "staging"
  )
  assert.equal(
    parseTelemetryConfig({
      VITE_SENTRY_ENVIRONMENT: " staging ",
      VITE_SENTRY_RELEASE: " abc123 ",
    }).release,
    "abc123"
  )
  for (const [raw, expected] of [
    ["0", 0],
    ["1", 1],
    ["0.3", 0.3],
    ["-1", 0.1],
    ["2", 0.1],
    ["NaN", 0.1],
    ["", 0.1],
  ] as const) {
    assert.equal(
      parseTelemetryConfig({ VITE_SENTRY_TRACES_SAMPLE_RATE: raw })
        .tracesSampleRate,
      expected
    )
  }
})

test("only same origin API URLs receive request tracing or propagation", () => {
  const targets = telemetryTraceTargets(origin)
  for (const [url, allowed] of [
    ["/api", true],
    ["/api/sessions?token=private", true],
    [`${origin}/api/sessions`, true],
    ["//trellis.test/api/settings", true],
    ["//supabase.test/api/settings", false],
    ["https://trellis.test.evil.test/api", false],
    ["https://trellis.test@evil.test/api", false],
    ["/apiary", false],
    ["/assets/index.js", false],
    ["data:text/plain,private", false],
  ] as const) {
    assert.equal(isSameOriginApiRequest(url, origin), allowed, url)
    if (!url.startsWith("//")) {
      assert.equal(
        targets.some((target) => target.test(url)),
        allowed,
        url
      )
    }
  }
})

test("dynamic API values and arbitrary routes never become operation names", () => {
  assert.equal(
    normalizeTelemetryPath(
      "/api/sessions/private-session/runs/private-run/events?draft=secret#oauth"
    ),
    "/api/sessions/:id/runs/:id/events"
  )
  assert.equal(
    normalizeTelemetryPath("/api/sessions/private/history"),
    "/api/sessions/:id/history"
  )
  assert.equal(
    normalizeTelemetryPath("/api/onboarding/steps/profile"),
    "/api/onboarding/steps/profile"
  )
  assert.equal(normalizeTelemetryPath("/api/private-new-route"), "/api/:path")
  assert.equal(normalizeTelemetryPath("/private-chat-title"), "/:path")
})

test("expected failures are quiet while outages and timeouts remain actionable", () => {
  for (const error of [
    { name: "AbortError" },
    { name: "CancelledError" },
    { name: "AuthAccessError", code: "reauth_required" },
    { name: "ApiError", status: 401, code: "authentication_required" },
    { name: "ApiError", status: 403 },
    { name: "ApiError", status: 422 },
    { name: "ApiError", status: 429 },
    { name: "RuntimeError", code: "run_cancelled" },
    { name: "RuntimeError", code: "run_interrupted" },
    { name: "RuntimeError", code: "budget_exceeded" },
    ...[
      "run_budget_exhausted",
      "budget_cost_limit",
      "model_call_limit",
      "tool_call_limit",
      "token_limit",
      "cost_limit",
      "time_limit",
      "approval_denied",
      "context_input_too_large",
    ].map((code) => ({ name: "RuntimeError", code })),
    { name: "RuntimeError", code: "provider_not_configured" },
  ])
    assert.equal(classifyTelemetryFailure(error), null)
  assert.equal(
    classifyTelemetryFailure({ name: "ApiError", status: 503 }),
    "service_unavailable"
  )
  assert.equal(
    classifyTelemetryFailure({
      name: "AuthAccessError",
      code: "auth_unavailable",
    }),
    "auth_unavailable"
  )
  assert.equal(
    classifyTelemetryFailure({
      name: "RuntimeError",
      code: "runtime_protocol_error",
    }),
    "runtime_protocol_error"
  )
  assert.equal(
    classifyTelemetryFailure({ name: "ApiError", status: 408 }),
    "request_timeout"
  )
  assert.equal(
    classifyTelemetryFailure(new Error("arbitrary message")),
    "unexpected_failure"
  )
})

test("query cancellations stay quiet when their constructors are renamed", () => {
  class MinifiedCancellation extends CancelledError {}
  Object.defineProperty(MinifiedCancellation, "name", { value: "a" })
  const error = new MinifiedCancellation()
  assert.equal(error.name, "Error")
  assert.equal(classifyTelemetryFailure(error), null)
})

test("event reconstruction removes hostile fields including linked exception payloads", () => {
  const secret = "draft-token-email@example.test"
  const event = sanitizeTelemetryEvent(
    {
      event_id: "a".repeat(32),
      timestamp: 123,
      level: "warning",
      message: secret,
      user: { id: secret, email: secret },
      request: {
        url: `${origin}/api/sessions/${secret}?token=${secret}#${secret}`,
        headers: { Authorization: secret },
        data: secret,
      },
      tags: { secret, "telemetry.operation": "context.preview" },
      extra: { draft: secret },
      contexts: {
        arbitrary: { secret },
        trace: {
          trace_id: "b".repeat(32),
          span_id: "c".repeat(16),
          data: { secret },
        },
      },
      exception: {
        values: [
          {
            type: "TypeError",
            value: secret,
            mechanism: { type: "generic", handled: true, data: { secret } },
            stacktrace: {
              frames: [
                {
                  filename: `${origin}/assets/index-12345678.js?token=${secret}#${secret}`,
                  lineno: 12,
                  colno: 8,
                  function: secret,
                  vars: { secret },
                  context_line: secret,
                },
              ],
            },
          },
          { type: "Error", value: secret },
        ],
      },
      debug_meta: {
        images: [
          {
            type: "sourcemap",
            code_file: `${origin}/assets/index-12345678.js?token=${secret}`,
            debug_id: "12345678-1234-1234-1234-123456789abc",
          },
        ],
      },
    },
    { originalException: new TypeError(secret) },
    config
  )
  assert.ok(event)
  assert.equal(JSON.stringify(event).includes(secret), false)
  assert.equal(event.exception?.values?.[0]?.type, "TypeError")
  assert.equal(
    event.exception?.values?.[0]?.stacktrace?.frames?.[0]?.lineno,
    12
  )
  assert.equal(
    event.exception?.values?.[0]?.stacktrace?.frames?.[0]?.filename,
    `${origin}/assets/index-12345678.js`
  )
  assert.equal(
    event.debug_meta?.images?.[0]?.debug_id,
    "12345678-1234-1234-1234-123456789abc"
  )
  assert.equal(event.tags?.["telemetry.operation"], "context.preview")
  assert.equal(event.level, "warning")
})

test("streamed spans retain scalar measurements and discard arbitrary attributes and links", () => {
  const secret = "draft-and-oauth-token"
  const span = sanitizeTelemetrySpan(
    {
      trace_id: "a".repeat(32),
      span_id: "b".repeat(16),
      parent_span_id: "c".repeat(16),
      name: `POST ${origin}/api/sessions/${secret}/runs/${secret}/events?token=${secret}#${secret}`,
      start_timestamp: 1,
      end_timestamp: 2,
      status: "ok",
      is_segment: true,
      attributes: {
        "sentry.op": "http.client",
        "http.request.method": "POST",
        "http.response.status_code": 503,
        "url.full": `${origin}/api/sessions/${secret}/runs/${secret}/events?token=${secret}`,
        "gen_ai.input.messages": secret,
        "custom.secret": secret,
        "browser.name": secret,
      },
      links: [
        {
          trace_id: "c".repeat(32),
          span_id: "d".repeat(16),
          attributes: { secret },
        },
      ],
    },
    origin
  )
  assert.equal(JSON.stringify(span).includes(secret), false)
  assert.equal(span.name, "POST /api/sessions/:id/runs/:id/events")
  assert.equal(span.attributes["http.response.status_code"], 503)
  assert.equal(span.links, undefined)
  assert.equal(span.end_timestamp, 2)
})

test("chat URLs become a route template in exported error URLs and browser spans", () => {
  const chatId = "11111111-2222-4333-a444-555555555555"
  const privateUrl = `${origin}/chats/${chatId}?code=private#private`
  assert.equal(normalizeTelemetryPath(privateUrl), "/chats/:chatId")
  const event = sanitizeTelemetryEvent(
    { request: { url: privateUrl } },
    { originalException: new Error(chatId) },
    config
  )
  assert.equal(event?.request?.url, "/chats/:chatId")
  for (const op of ["pageload", "navigation"]) {
    const span = sanitizeTelemetrySpan(
      {
        name: privateUrl,
        attributes: { "sentry.op": op, "url.full": privateUrl },
        trace_id: "1".repeat(32),
        span_id: "2".repeat(16),
        start_timestamp: 1,
      },
      origin
    )
    assert.equal(span.name, "/chats/:chatId")
    assert.equal(JSON.stringify({ event, span }).includes(chatId), false)
  }
})
