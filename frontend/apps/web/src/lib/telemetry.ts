import * as Sentry from "@sentry/react"
import type { RootOptions } from "react-dom/client"

import {
  classifyTelemetryFailure,
  isSameOriginApiRequest,
  normalizeTelemetryPath,
  sanitizeTelemetryDsc,
  sanitizeTelemetryEvent,
  sanitizeTelemetrySpan,
  telemetryTraceTargets,
  type TelemetryConfig,
  type TelemetryOperation,
} from "./telemetry-policy"

export type { TelemetryConfig, TelemetryOperation } from "./telemetry-policy"

const reportedErrors = new WeakSet<object>()
const excludedIntegrations = new Set([
  "Breadcrumbs",
  "Console",
  "ConversationId",
  "Dedupe",
  "HttpContext",
  "CultureContext",
  "BrowserSession",
])

type BrowserTracingOptions = NonNullable<
  Parameters<typeof Sentry.browserTracingIntegration>[0]
>

export function telemetryBrowserTracingOptions(
  config: TelemetryConfig
): BrowserTracingOptions {
  return {
    beforeStartSpan: (options) => ({
      ...options,
      name: normalizeTelemetryPath(
        typeof window === "undefined" ? options.name : window.location.pathname
      ),
    }),
    shouldCreateSpanForRequest: (url: string) =>
      isSameOriginApiRequest(url, config.origin),
  }
}

export function createTelemetryOptions(
  config: TelemetryConfig
): Sentry.BrowserOptions {
  return {
    enabled: config.enabled,
    dsn: config.dsn,
    environment: config.environment,
    release: config.release,
    sampleRate: 1,
    tracesSampleRate: config.tracesSampleRate,
    sendClientReports: false,
    dataCollection: {
      userInfo: false,
      cookies: false,
      httpHeaders: false,
      httpBodies: [],
      urlQueryParams: false,
      graphQL: { document: false, variables: false },
      genAI: { inputs: false, outputs: false },
      databaseQueryData: false,
      queues: false,
      stackFrameVariables: false,
      frameContextLines: 0,
    },
    maxBreadcrumbs: 0,
    integrations: (defaults) => [
      ...defaults.filter(
        (integration) => !excludedIntegrations.has(integration.name)
      ),
      {
        name: "TrellisPrivacy",
        setup(client) {
          client.on("prepareSpanScope", ({ scope }) => {
            const context = scope.getPropagationContext()
            if (context.dsc) {
              scope.setPropagationContext({
                ...context,
                dsc: sanitizeTelemetryDsc(context.dsc, config),
              })
            }
          })
          client.on("createDsc", (dsc) => {
            const safe = sanitizeTelemetryDsc(dsc, config)
            for (const key of Object.keys(dsc))
              delete dsc[key as keyof typeof dsc]
            Object.assign(dsc, safe)
          })
          client.on("beforeEnvelope", (envelope) => {
            if (envelope[0].trace)
              envelope[0].trace = sanitizeTelemetryDsc(
                envelope[0].trace,
                config
              )
            for (let index = envelope[1].length - 1; index >= 0; index--) {
              const type = envelope[1][index]![0].type
              if (type !== "event" && type !== "span")
                envelope[1].splice(index, 1)
            }
          })
        },
      },
      ...(typeof window !== "undefined" && typeof document !== "undefined"
        ? [
            Sentry.browserTracingIntegration(
              telemetryBrowserTracingOptions(config)
            ),
          ]
        : []),
    ],
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,
    profileSessionSampleRate: 0,
    beforeBreadcrumb: () => null,
    beforeSend: (event, hint) => sanitizeTelemetryEvent(event, hint, config),
    beforeSendSpan: (span) => sanitizeTelemetrySpan(span, config.origin),
    beforeSendLog: () => null,
    beforeSendMetric: () => null,
    tracePropagationTargets: telemetryTraceTargets(config.origin),
  }
}

export function initializeTelemetry(config: TelemetryConfig): void {
  if (!config.enabled || Sentry.getClient()) return
  Sentry.init(createTelemetryOptions(config))
}

export function telemetryRootOptions(): RootOptions | undefined {
  if (!Sentry.isEnabled()) return undefined
  return {
    onCaughtError: Sentry.reactErrorHandler(() => undefined),
    onUncaughtError: Sentry.reactErrorHandler(),
    onRecoverableError(error, errorInfo) {
      Sentry.withScope((scope) => {
        scope.setLevel("warning")
        Sentry.reactErrorHandler(() => undefined)(error, errorInfo)
      })
    },
  }
}

export type RuntimeTelemetryMethod =
  "run.start" | "run.subscribe" | "run.cancel" | "run.respond"
type RuntimeTelemetryOutcome = "ok" | "error" | "timeout" | "cancelled"

export function startRuntimeRequestSpan(
  method: RuntimeTelemetryMethod
): (outcome: RuntimeTelemetryOutcome) => void {
  if (!Sentry.isEnabled()) return () => undefined
  const span = Sentry.startInactiveSpan({ name: method, op: "rpc.client" })
  let ended = false
  return (outcome: RuntimeTelemetryOutcome): void => {
    if (ended) return
    ended = true
    span.setStatus(
      outcome === "ok"
        ? { code: 1 }
        : {
            code: 2,
            message:
              outcome === "timeout"
                ? "deadline_exceeded"
                : outcome === "cancelled"
                  ? "cancelled"
                  : "internal_error",
          }
    )
    span.end()
  }
}

export function reportFailure(
  error: unknown,
  operation: TelemetryOperation
): void {
  const client = Sentry.getClient()
  if (
    !client ||
    client.getOptions().enabled === false ||
    classifyTelemetryFailure(error) === null
  )
    return
  if (
    (typeof error === "object" && error !== null) ||
    typeof error === "function"
  ) {
    if (reportedErrors.has(error)) return
    reportedErrors.add(error)
  }
  Sentry.captureException(error, { tags: { "telemetry.operation": operation } })
}
