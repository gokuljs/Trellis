import { expect, test } from "bun:test"

import { restoreRunActivity } from "../src/lib/activity-history"
import { describeActivity } from "../src/lib/activity-status"
import type { LatestRun } from "../src/lib/app-types"
import type { TurnRunActivity } from "../src/lib/runtime-client"

const latest: LatestRun = {
  run_id: "run",
  turn_id: "turn",
  status: "queued",
  last_sequence: 1,
}

test("restored queued snapshot remains queued before the socket catches up", () => {
  const activity = restoreRunActivity(undefined, latest)
  expect(activity.events).toEqual([])
  expect(
    describeActivity(
      [],
      true,
      Date.now(),
      undefined,
      undefined,
      activity.runInfo?.status
    ).label
  ).toBe("Queued")
})

test("reconnecting to the same run keeps its already observed progress", () => {
  const activity: TurnRunActivity = {
    events: [
      {
        runId: "run",
        sequence: 2,
        eventType: "run.started",
        eventVersion: 1,
        data: {},
      },
    ],
    runInfo: { runId: "run", modelId: "locked-model" },
  }
  const restored = restoreRunActivity(activity, {
    ...latest,
    status: "running",
    last_sequence: 2,
  })
  expect(restored.events).toEqual(activity.events)
  expect(restored.runInfo).toEqual({
    runId: "run",
    modelId: "locked-model",
    status: "running",
  })
})

test("an explicit retry separates the prior run's events from the new run", () => {
  const previous: TurnRunActivity = {
    events: [
      {
        runId: "older-run",
        sequence: 4,
        eventType: "run.interrupted",
        eventVersion: 1,
        data: {},
      },
    ],
    runInfo: { runId: "older-run" },
  }
  const restored = restoreRunActivity(previous, latest)
  expect(restored.events).toEqual([])
  expect(restored.priorAttempts?.map((attempt) => attempt.runId)).toEqual([
    "older-run",
  ])
  expect(restored.runInfo?.runId).toBe("run")
})
