export const logOperations = [
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

export type LogOperation = (typeof logOperations)[number]

export const logEvents = [
  "application.started",
  "operation.failed",
  "react.error.caught",
  "react.error.uncaught",
  "react.error.recoverable",
] as const

export type LogEvent = (typeof logEvents)[number]

export const logFailures = [
  "unexpected_failure",
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

export type LogFailure = (typeof logFailures)[number]

export type LogFields = {
  operation?: LogOperation
  failure?: LogFailure
  status?: number
}

type LogLevel = "info" | "warning" | "error"
const allowedOperations = new Set<string>(logOperations)
const allowedEvents = new Set<string>(logEvents)
const allowedFailures = new Set<string>(logFailures)

function safeFields(fields: LogFields = {}): LogFields {
  return {
    ...(fields.operation && allowedOperations.has(fields.operation)
      ? { operation: fields.operation }
      : {}),
    ...(fields.failure && allowedFailures.has(fields.failure)
      ? { failure: fields.failure }
      : {}),
    ...(typeof fields.status === "number" &&
    Number.isInteger(fields.status) &&
    fields.status >= 0 &&
    fields.status <= 599
      ? { status: fields.status }
      : {}),
  }
}

function write(level: LogLevel, event: LogEvent, fields: LogFields = {}) {
  const record = {
    timestamp: new Date().toISOString(),
    level,
    event: allowedEvents.has(event) ? event : "operation.failed",
    ...safeFields(fields),
  }
  const output =
    level === "error"
      ? console.error
      : level === "warning"
        ? console.warn
        : console.info
  output.call(console, record)
}

export const logger = {
  info(event: LogEvent, fields?: LogFields) {
    write("info", event, fields)
  },
  warning(event: LogEvent, fields?: LogFields) {
    write("warning", event, fields)
  },
  error(event: LogEvent, fields?: LogFields) {
    write("error", event, fields)
  },
}
