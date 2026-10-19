import assert from "node:assert/strict"
import { afterEach, mock, spyOn, test } from "bun:test"
import { CancelledError } from "@tanstack/react-query"

import { logFailure, loggingRootOptions } from "../src/lib/logging"
import { logger } from "../src/lib/logger"

const privateText = "private-prompt-token-and-workspace-path"
const records: Record<string, unknown>[] = []

afterEach(() => {
  records.length = 0
  mock.restore()
})

function captureConsole() {
  spyOn(console, "info").mockImplementation((record: unknown) => {
    records.push(record as Record<string, unknown>)
  })
  spyOn(console, "warn").mockImplementation((record: unknown) => {
    records.push(record as Record<string, unknown>)
  })
  spyOn(console, "error").mockImplementation((record: unknown) => {
    records.push(record as Record<string, unknown>)
  })
}

test("logger maps levels to structured console records", () => {
  captureConsole()
  logger.info("application.started")
  logger.warning("react.error.recoverable", {
    failure: "unexpected_failure",
  })
  logger.error("operation.failed", {
    operation: "settings.load",
    failure: "service_unavailable",
    status: 503,
  })

  assert.deepEqual(
    records.map(({ level, event, operation, failure, status }) => ({
      level,
      event,
      operation,
      failure,
      status,
    })),
    [
      {
        level: "info",
        event: "application.started",
        operation: undefined,
        failure: undefined,
        status: undefined,
      },
      {
        level: "warning",
        event: "react.error.recoverable",
        operation: undefined,
        failure: "unexpected_failure",
        status: undefined,
      },
      {
        level: "error",
        event: "operation.failed",
        operation: "settings.load",
        failure: "service_unavailable",
        status: 503,
      },
    ]
  )
  assert.match(String(records[0]?.timestamp), /^\d{4}-\d\d-\d\dT.*Z$/)
})

test("failure records keep only fixed codes and bounded status", () => {
  captureConsole()
  const error = Object.assign(new Error(privateText), {
    code: "service_unavailable",
    status: 503,
  })
  logFailure(error, "context.preview")

  assert.deepEqual(
    records.map(({ level, event, operation, failure, status }) => ({
      level,
      event,
      operation,
      failure,
      status,
    })),
    [
      {
        level: "error",
        event: "operation.failed",
        operation: "context.preview",
        failure: "service_unavailable",
        status: 503,
      },
    ]
  )
  assert.equal(JSON.stringify(records).includes(privateText), false)

  const invalidStatus = Object.assign(new Error(privateText), { status: 700 })
  logFailure(invalidStatus, "settings.load")
  assert.equal(records[1]?.status, undefined)
  assert.equal(JSON.stringify(records).includes(privateText), false)
})

test("logger drops unrecognized metadata even when a caller bypasses its types", () => {
  captureConsole()
  const unsafeLogger = logger.error as unknown as (
    event: string,
    fields: Record<string, unknown>
  ) => void
  unsafeLogger("operation.failed", {
    operation: "settings.load",
    failure: privateText,
    status: 900,
    prompt: privateText,
  })

  assert.equal(records.length, 1)
  assert.equal(records[0]?.event, "operation.failed")
  assert.equal(records[0]?.operation, "settings.load")
  assert.equal(records[0]?.failure, undefined)
  assert.equal(records[0]?.status, undefined)
  assert.equal("prompt" in records[0]!, false)
  assert.equal(JSON.stringify(records).includes(privateText), false)
})

test("authentication, cancellation, and expected client failures stay quiet", () => {
  captureConsole()
  logFailure(new CancelledError(), "run.cancel")
  logFailure(
    Object.assign(new Error(privateText), { name: "AbortError" }),
    "run.start"
  )
  logFailure(
    Object.assign(new Error(privateText), {
      name: "ApiError",
      code: "authentication_required",
      status: 401,
    }),
    "sessions.open"
  )
  logFailure(
    Object.assign(new Error(privateText), {
      name: "ApiError",
      code: "session_not_found",
      status: 404,
    }),
    "history.load"
  )

  assert.deepEqual(records, [])
})

test("duplicate failures log once and React hooks preserve error levels", () => {
  captureConsole()
  const shared = new Error(privateText)
  logFailure(shared, "sessions.open")
  logFailure(shared, "history.load")

  const root = loggingRootOptions()
  root.onCaughtError?.(new Error(privateText), {} as never)
  root.onUncaughtError?.(new Error(privateText), {} as never)
  root.onRecoverableError?.(new Error(privateText), {} as never)

  assert.deepEqual(
    records.map(({ level, event }) => ({ level, event })),
    [
      { level: "error", event: "operation.failed" },
      { level: "error", event: "react.error.caught" },
      { level: "error", event: "react.error.uncaught" },
      { level: "warning", event: "react.error.recoverable" },
    ]
  )
  assert.equal(JSON.stringify(records).includes(privateText), false)
})
