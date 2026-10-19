import { CancelledError } from "@tanstack/react-query"
import type { RootOptions } from "react-dom/client"

import {
  logger,
  type LogEvent,
  type LogFailure,
  type LogOperation,
} from "./logger"

const reportedErrors = new WeakSet<object>()
const quietCodes = new Set([
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
const knownFailures = new Set<LogFailure>([
  "auth_unavailable",
  "service_unavailable",
  "request_timeout",
  "runtime_protocol_error",
  "connection_lost",
  "runtime_request_failed",
  "run_failed",
  "provider_timeout",
  "provider_unavailable",
])

type FailureDetails = {
  failure: LogFailure
  status?: number
}

function fieldsOf(error: unknown): Record<string, unknown> {
  return error !== null && typeof error === "object"
    ? (error as Record<string, unknown>)
    : {}
}

function safeStatus(value: unknown): number | undefined {
  return typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 0 &&
    value <= 599
    ? value
    : undefined
}

function classifyFailure(error: unknown): FailureDetails | null {
  const fields = fieldsOf(error)
  if (
    fields.name === "AbortError" ||
    fields.name === "CancelledError" ||
    error instanceof CancelledError
  )
    return null

  const code = fields.code
  const status = safeStatus(fields.status)
  if (typeof code === "string" && quietCodes.has(code)) return null
  if (fields.name === "TimeoutError")
    return {
      failure: "request_timeout",
      ...(status === undefined ? {} : { status }),
    }
  if (typeof code === "string" && knownFailures.has(code as LogFailure))
    return {
      failure: code as LogFailure,
      ...(status === undefined ? {} : { status }),
    }
  if (status !== undefined && status >= 400 && status < 500 && status !== 408)
    return null
  if (status === 408) return { failure: "request_timeout", status }
  if (status === 0 || (status !== undefined && status >= 500))
    return { failure: "service_unavailable", status }
  return {
    failure: "unexpected_failure",
    ...(status === undefined ? {} : { status }),
  }
}

function logError(
  error: unknown,
  event: LogEvent,
  operation?: LogOperation,
  level: "warning" | "error" = "error"
): void {
  const details = classifyFailure(error)
  if (!details) return
  if (
    (typeof error === "object" && error !== null) ||
    typeof error === "function"
  ) {
    if (reportedErrors.has(error)) return
    reportedErrors.add(error)
  }
  const fields = {
    ...(operation === undefined ? {} : { operation }),
    ...details,
  }
  if (level === "warning") logger.warning(event, fields)
  else logger.error(event, fields)
}

export function logFailure(error: unknown, operation: LogOperation): void {
  logError(error, "operation.failed", operation)
}

export function loggingRootOptions(): RootOptions {
  return {
    onCaughtError(error) {
      logError(error, "react.error.caught")
    },
    onUncaughtError(error) {
      logError(error, "react.error.uncaught")
    },
    onRecoverableError(error) {
      logError(error, "react.error.recoverable", undefined, "warning")
    },
  }
}
