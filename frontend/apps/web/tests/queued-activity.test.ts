import { expect, test } from "bun:test"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"

import { RunActivityDisclosure } from "../src/components/run-activity-disclosure"
import { describeActivity } from "../src/lib/activity-status"
import type { RuntimeRunEvent } from "../src/lib/runtime-client"

const queued: RuntimeRunEvent = {
  runId: "run",
  sequence: 1,
  eventType: "run.queued",
  eventVersion: 1,
  data: { status: "queued" },
}
const started: RuntimeRunEvent = {
  ...queued,
  sequence: 2,
  eventType: "run.started",
  data: { status: "running" },
}

function render(events: RuntimeRunEvent[]) {
  return renderToStaticMarkup(
    createElement(RunActivityDisclosure, {
      activity: { events, runInfo: null },
      pending: true,
      expanded: false,
      onToggle: () => undefined,
      onClose: () => undefined,
      approvalPendingToolId: null,
      approvalError: null,
      onApprovalDecision: () => undefined,
    })
  )
}

test("queued work has an understandable saved-request capacity message", () => {
  expect(describeActivity([queued], true, Date.now()).label).toBe("Queued")
  const markup = render([queued])
  expect(markup).toContain('role="status"')
  expect(markup).toContain("Your request is saved and queued.")
  expect(markup).toContain("execution capacity")
  expect(markup).toContain("You can close this page.")
  expect(markup).not.toContain("Deadline")
})

test("running work replaces the queue message with execution activity", () => {
  expect(describeActivity([queued, started], true, Date.now()).label).toBe(
    "Thinking"
  )
  const markup = render([queued, started])
  expect(markup).toContain("Thinking")
  expect(markup).not.toContain("Your request is saved and queued.")
})
