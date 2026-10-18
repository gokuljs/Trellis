import type {
  ErrorEvent,
  BrowserOptions,
  EventHint,
  Exception,
  StackFrame,
} from "@sentry/react"
import { CancelledError } from "@tanstack/react-query"

type StreamedSpanJSON = Parameters<
  NonNullable<BrowserOptions["beforeSendSpan"]>
>[0]

export const telemetryOperations = [
  "workspace.bootstrap",
  "settings.load",
  "settings.save",
  "onboarding.save",
  "sessions.list",
  "sessions.create",
  "sessions.open",
  "history.load",
  "activity.summaries",
  "activity.events",
  "context.preview",
  "run.start",
  "run.restore",
  "run.cancel",
  "run.respond",
] as const

export type TelemetryOperation = (typeof telemetryOperations)[number]

export interface TelemetryEnvironment {
  MODE?: string
  PROD?: boolean
  VITE_SENTRY_DSN?: string
  VITE_SENTRY_ENABLED?: string
  VITE_SENTRY_ENVIRONMENT?: string
  VITE_SENTRY_RELEASE?: string
  VITE_SENTRY_TRACES_SAMPLE_RATE?: string
  origin?: string
}

export interface TelemetryConfig {
  enabled: boolean
  dsn?: string
  environment: string
  release?: string
  tracesSampleRate: number
  origin: string
}

function parseUrl(value: string, origin?: string): URL | null {
  try {
    return new URL(value, origin || "https://telemetry.invalid")
  } catch {
    return null
  }
}

export function parseTelemetryConfig(
  env: TelemetryEnvironment
): TelemetryConfig {
  const mode = env.MODE || "development"
  const environment = env.VITE_SENTRY_ENVIRONMENT?.trim() || mode
  const rawDsn = env.VITE_SENTRY_DSN?.trim()
  const dsn = parseUrl(rawDsn || "")
  const validDsn = !!(
    rawDsn &&
    dsn &&
    ["https:", "http:"].includes(dsn.protocol) &&
    /^[\w]+$/.test(dsn.username) &&
    !dsn.password &&
    !dsn.search &&
    !dsn.hash &&
    /\/\d+$/.test(dsn.pathname)
  )
  const rawRate = env.VITE_SENTRY_TRACES_SAMPLE_RATE?.trim()
  const rate = rawRate ? Number(rawRate) : NaN
  const automatic =
    env.PROD === true && mode === "production" && environment === "production"
  const rawEnabled = env.VITE_SENTRY_ENABLED?.trim()
  const enabled =
    validDsn && (rawEnabled === "true" || (!rawEnabled && automatic))
  const release = env.VITE_SENTRY_RELEASE?.trim()
  const origin = env.origin ? parseUrl(env.origin)?.origin : undefined
  return {
    enabled,
    dsn: validDsn ? dsn!.toString() : undefined,
    environment: /^[\w.-]{1,64}$/.test(environment) ? environment : mode,
    release: release && /^[\w.@+-]{1,200}$/.test(release) ? release : undefined,
    tracesSampleRate:
      Number.isFinite(rate) && rate >= 0 && rate <= 1 ? rate : 0.1,
    origin: origin && origin !== "null" ? origin : "",
  }
}

const apiPaths = [
  /^\/api$/,
  /^\/api\/(?:profile|settings|context-preview|onboarding|sessions|session-pages|runtime)$/,
  /^\/api\/settings\/(?:model|budget)$/,
  /^\/api\/onboarding\/steps\/(?:intro|profile|model)$/,
]

export function normalizeTelemetryPath(value: string): string {
  const path = parseUrl(value)?.pathname || ""
  if (path === "/") return "/"
  if (/^\/chats\/[^/]+$/.test(path)) return "/chats/:chatId"
  if (apiPaths.some((pattern) => pattern.test(path))) return path
  const session = path.match(
    /^\/api\/sessions\/[^/]+(\/(?:history|model|runs(?:\/latest)?))?$/
  )
  if (session) return `/api/sessions/:id${session[1] || ""}`
  if (/^\/api\/sessions\/[^/]+\/runs\/[^/]+\/events$/.test(path))
    return "/api/sessions/:id/runs/:id/events"
  return path === "/api" || path.startsWith("/api/") ? "/api/:path" : "/:path"
}

export function isSameOriginApiRequest(value: string, origin: string): boolean {
  const url = parseUrl(value, origin)
  return (
    !!origin &&
    url?.origin === origin &&
    (url.pathname === "/api" || url.pathname.startsWith("/api/"))
  )
}

export function telemetryTraceTargets(origin: string): RegExp[] {
  if (!origin) return []
  const escaped = origin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
  return [/^\/api(?:\/|[?#]|$)/, new RegExp(`^${escaped}/api(?:/|[?#]|$)`)]
}

const expectedCodes = new Set([
  "authentication_required",
  "reauth_required",
  "auth_required",
  "auth_failed",
  "cloud_access_denied",
  "cancelled",
  "canceled",
  "user_cancelled",
  "run_cancelled",
  "run_finished",
  "interrupted",
  "run_interrupted",
  "runtime_shutdown",
  "budget_exceeded",
  "budget_limit_exceeded",
  "run_budget_exhausted",
  "budget_cost_limit",
  "model_call_limit",
  "tool_call_limit",
  "token_limit",
  "cost_limit",
  "time_limit",
  "approval_denied",
  "context_input_too_large",
  "provider_not_configured",
])
const failureCodes = [
  "auth_unavailable",
  "service_unavailable",
  "request_timeout",
  "runtime_protocol_error",
  "connection_lost",
  "runtime_request_failed",
  "run_failed",
  "provider_timeout",
  "provider_unavailable",
] as const

export type TelemetryFailure =
  "unexpected_failure" | (typeof failureCodes)[number]

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {}
}

export function classifyTelemetryFailure(
  error: unknown
): TelemetryFailure | null {
  const fields = record(error)
  if (
    fields.name === "AbortError" ||
    fields.name === "CancelledError" ||
    error instanceof CancelledError
  )
    return null
  if (fields.name === "TimeoutError") return "request_timeout"
  const domainError = ["ApiError", "RuntimeError", "AuthAccessError"].includes(
    String(fields.name)
  )
  if (!domainError) return "unexpected_failure"
  if (typeof fields.code === "string" && expectedCodes.has(fields.code))
    return null
  const status = typeof fields.status === "number" ? fields.status : undefined
  if (status && status >= 400 && status < 500 && status !== 408) return null
  if (
    typeof fields.code === "string" &&
    failureCodes.includes(fields.code as (typeof failureCodes)[number])
  )
    return fields.code as TelemetryFailure
  if (status === 408) return "request_timeout"
  if (status === 0 || (status && status >= 500)) return "service_unavailable"
  return "unexpected_failure"
}

export function sanitizeTelemetryDsc(raw: unknown, config: TelemetryConfig) {
  const value = record(raw)
  const dsn = parseUrl(config.dsn || "")
  return {
    trace_id: identifier(value.trace_id, 32) || "0".repeat(32),
    public_key: dsn?.username || "",
    release: config.release,
    environment: config.environment,
    ...(value.sampled === "true" || value.sampled === "false"
      ? { sampled: value.sampled }
      : {}),
    ...(typeof value.sample_rate === "string" &&
    /^(?:0(?:\.\d+)?|1(?:\.0+)?)$/.test(value.sample_rate)
      ? { sample_rate: value.sample_rate }
      : {}),
    ...(typeof value.sample_rand === "string" &&
    /^0\.\d+$/.test(value.sample_rand)
      ? { sample_rand: value.sample_rand }
      : {}),
    ...(typeof value.org_id === "string" && /^\d+$/.test(value.org_id)
      ? { org_id: value.org_id }
      : {}),
  }
}

function identifier(value: unknown, length: number): string | undefined {
  return typeof value === "string" &&
    new RegExp(`^[a-f0-9]{${length}}$`, "i").test(value)
    ? value
    : undefined
}

function sourceFile(
  value: string | undefined,
  origin: string
): string | undefined {
  if (!value) return undefined
  const url = parseUrl(value, origin)
  if (!url || (url.origin !== origin && !value.startsWith("/src/")))
    return undefined
  if (
    !/^\/(?:assets\/[\w.-]+\.(?:m?js|css)|src\/[\w./-]+\.(?:[jt]sx?))$/.test(
      url.pathname
    )
  )
    return undefined
  return `${origin}${url.pathname}`
}

function sanitizeFrame(frame: StackFrame, origin: string): StackFrame {
  return {
    filename: sourceFile(frame.filename || frame.abs_path, origin),
    lineno: frame.lineno,
    colno: frame.colno,
    in_app: frame.in_app,
    debug_id:
      typeof frame.debug_id === "string" &&
      /^[a-f\d-]{36}$/i.test(frame.debug_id)
        ? frame.debug_id
        : undefined,
  }
}

const exceptionTypes = new Set([
  "Error",
  "TypeError",
  "ReferenceError",
  "SyntaxError",
  "RangeError",
  "URIError",
  "EvalError",
  "AggregateError",
  "DOMException",
  "TimeoutError",
  "ApiError",
  "RuntimeError",
  "AuthAccessError",
])
const mechanismTypes = new Set([
  "generic",
  "onerror",
  "onunhandledrejection",
  "instrument",
  "auto.function.react.error_handler",
  "auto.function.react.error_boundary",
  "auto.browser.global_handlers.onerror",
  "auto.browser.global_handlers.onunhandledrejection",
  "auto.browser.browserapierrors.setTimeout",
  "auto.browser.browserapierrors.setInterval",
  "auto.browser.browserapierrors.requestAnimationFrame",
  "auto.browser.browserapierrors.handleEvent",
  "auto.browser.browserapierrors.addEventListener",
  "auto.browser.browserapierrors.xhr.onload",
  "auto.browser.browserapierrors.xhr.onerror",
  "auto.browser.browserapierrors.xhr.onprogress",
  "auto.browser.browserapierrors.xhr.onreadystatechange",
])

function sanitizeSdk(sdk: ErrorEvent["sdk"]): ErrorEvent["sdk"] {
  if (!sdk) return undefined
  const names = new Set([
    "EventFilters",
    "FunctionToString",
    "BrowserApiErrors",
    "GlobalHandlers",
    "LinkedErrors",
    "TrellisPrivacy",
    "BrowserTracing",
  ])
  const packages = new Set([
    "npm:@sentry/react",
    "npm:@sentry/browser",
    "npm:@sentry/core",
  ])
  const version = (value: string | undefined) =>
    value && /^\d+\.\d+\.\d+(?:-[\w.]+)?$/.test(value) ? value : undefined
  return {
    name: sdk.name === "sentry.javascript.react" ? sdk.name : undefined,
    version: version(sdk.version),
    integrations: sdk.integrations?.filter((name) => names.has(name)),
    packages: sdk.packages?.flatMap((pkg) =>
      packages.has(pkg.name) && version(pkg.version)
        ? [{ name: pkg.name, version: pkg.version }]
        : []
    ),
    settings: { infer_ip: "never" },
  }
}

function sanitizeException(
  exception: Exception,
  classification: TelemetryFailure,
  origin: string
): Exception {
  const mechanism = exception.mechanism
  return {
    type:
      exception.type && exceptionTypes.has(exception.type)
        ? exception.type
        : "Error",
    value: classification,
    stacktrace: exception.stacktrace
      ? {
          frames: exception.stacktrace.frames?.map((frame) =>
            sanitizeFrame(frame, origin)
          ),
        }
      : undefined,
    mechanism: mechanism
      ? {
          type: mechanismTypes.has(mechanism.type) ? mechanism.type : "generic",
          handled: mechanism.handled,
          synthetic: mechanism.synthetic,
          is_exception_group: mechanism.is_exception_group,
          exception_id: mechanism.exception_id,
          parent_id: mechanism.parent_id,
        }
      : undefined,
  }
}

export function sanitizeTelemetryEvent(
  event: ErrorEvent,
  hint: EventHint,
  config: TelemetryConfig
): ErrorEvent | null {
  const classification = classifyTelemetryFailure(hint.originalException)
  if (classification === null) return null
  const operation = event.tags?.["telemetry.operation"]
  const trace = event.contexts?.trace
  const traceId = identifier(trace?.trace_id, 32)
  const spanId = identifier(trace?.span_id, 16)
  const status = record(hint.originalException).status
  return {
    type: undefined,
    event_id: identifier(event.event_id, 32),
    timestamp: event.timestamp,
    level: event.level,
    platform: "javascript",
    release: config.release,
    environment: config.environment,
    exception: event.exception
      ? {
          values: event.exception.values?.map((exception) =>
            sanitizeException(exception, classification, config.origin)
          ),
        }
      : undefined,
    message: event.exception ? undefined : classification,
    request: event.request?.url
      ? { url: normalizeTelemetryPath(event.request.url) }
      : undefined,
    tags: {
      "telemetry.failure": classification,
      ...(typeof status === "number" &&
      Number.isInteger(status) &&
      status >= 0 &&
      status <= 599
        ? { "telemetry.status": status }
        : {}),
      ...(typeof operation === "string" &&
      telemetryOperations.includes(operation as TelemetryOperation)
        ? { "telemetry.operation": operation }
        : {}),
    },
    contexts:
      traceId && spanId
        ? {
            trace: {
              trace_id: traceId,
              span_id: spanId,
              parent_span_id: identifier(trace?.parent_span_id, 16),
            },
          }
        : undefined,
    debug_meta: event.debug_meta
      ? {
          images: event.debug_meta.images?.flatMap((image) => {
            const codeFile = sourceFile(image.code_file, config.origin)
            return image.type === "sourcemap" &&
              codeFile &&
              /^[a-f\d-]{36}$/i.test(image.debug_id)
              ? [
                  {
                    type: "sourcemap" as const,
                    code_file: codeFile,
                    debug_id: image.debug_id,
                  },
                ]
              : []
          }),
        }
      : undefined,
    sdk: sanitizeSdk(event.sdk),
  }
}

const spanOps = new Set([
  "pageload",
  "navigation",
  "http.client",
  "resource.script",
  "resource.css",
  "resource.img",
  "resource.link",
  "resource.other",
  "browser",
  "ui.long-task",
  "ui.long-animation-frame",
  "measure",
  "rpc.client",
])
const httpMethods = new Set([
  "GET",
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
  "HEAD",
  "OPTIONS",
])
const scalarMeasurements = new Set([
  "http.response.status_code",
  "http.response.body.size",
  "http.request.body.size",
  "http.response_content_length",
  "http.response_transfer_size",
  "sentry.sample_rate",
  "sentry.segment.duration",
  "sentry.exclusive_time",
  "sentry.browser.dom_ready",
  "sentry.browser.load",
  "sentry.browser.connection.rtt",
])

export function sanitizeTelemetrySpan(
  span: StreamedSpanJSON,
  origin: string
): StreamedSpanJSON {
  const raw = span.attributes
  const op =
    typeof raw["sentry.op"] === "string" && spanOps.has(raw["sentry.op"])
      ? raw["sentry.op"]
      : "browser"
  const url = typeof raw["url.full"] === "string" ? raw["url.full"] : undefined
  const rawMethod = raw["http.request.method"]
  const method =
    typeof rawMethod === "string" && httpMethods.has(rawMethod)
      ? rawMethod
      : "GET"
  const rpc = [
    "run.start",
    "run.subscribe",
    "run.cancel",
    "run.respond",
  ].includes(span.name)
    ? span.name
    : "runtime.request"
  const path = normalizeTelemetryPath(url || span.name.replace(/^[A-Z]+ /, ""))
  const name =
    op === "http.client"
      ? `${method} ${path}`
      : op === "rpc.client"
        ? rpc
        : op === "pageload" || op === "navigation"
          ? path
          : op
  const attributes: StreamedSpanJSON["attributes"] = { "sentry.op": op }
  for (const key of scalarMeasurements) {
    const value = raw[key]
    if (typeof value === "number" && Number.isFinite(value))
      attributes[key] = value
  }
  if (op === "http.client") {
    attributes["http.request.method"] = method
    if (url && isSameOriginApiRequest(url, origin))
      attributes["url.path"] = normalizeTelemetryPath(url)
  }
  return {
    trace_id: span.trace_id,
    parent_span_id: span.parent_span_id,
    span_id: span.span_id,
    name,
    start_timestamp: span.start_timestamp,
    end_timestamp: span.end_timestamp,
    status: span.status,
    is_segment: span.is_segment,
    attributes,
  }
}
