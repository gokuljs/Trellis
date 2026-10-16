import { useEffect, useState } from "react"

import type { RunSummary } from "@/lib/app-types"
import { describeActivity } from "@/lib/activity-status"
import { activityTotals } from "@/lib/activity-summary"

import type {
  RuntimeRunEvent,
  RuntimeRunInfo,
  ToolApprovalDecision,
} from "@/lib/runtime-client"
import { groupRunEvents } from "@/lib/run-event-groups"
import { RunEventLog, RunEventTimestamp } from "@/components/run-event-log"
import "./run-activity.css"

type RunActivityProps = {
  label?: string
  events: RuntimeRunEvent[]
  runInfo: RuntimeRunInfo | null
  summary?: RunSummary | null
  summaryError?: string | null
  onRetrySummary?: () => void
  pending: boolean
  approvalPendingToolId: string | null
  approvalError: string | null
  onApprovalDecision: (
    toolCallId: string,
    decision: ToolApprovalDecision
  ) => void
}

const numberFormat = new Intl.NumberFormat("en-US")

function textField(data: Record<string, unknown>, key: string): string | null {
  return typeof data[key] === "string" ? data[key] : null
}

function numberField(
  data: Record<string, unknown>,
  key: string
): number | null {
  const value = data[key]
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function recordField(
  data: Record<string, unknown>,
  key: string
): Record<string, unknown> | null {
  const value = data[key]
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function estimatedCost(value: number) {
  return `$${value.toFixed(value > 0 && value < 0.01 ? 4 : 2)}`
}

function budgetLabel(preset: string | undefined) {
  if (preset === "conservative") return "Standard budget"
  if (preset === "longer") return "Extended budget"
  return "Run budget"
}

function failureTitle(code: string | null) {
  const titles: Record<string, string> = {
    model_call_limit: "Model call limit reached",
    tool_call_limit: "Tool limit reached",
    token_limit: "Token limit reached",
    cost_limit: "Cost limit reached",
    time_limit: "Time limit reached",
    provider_timeout: "Time limit reached",
  }
  return (code && titles[code]) || "Run failed"
}

function previewDetails(preview: Record<string, unknown> | null) {
  if (!preview) return null
  const path = textField(preview, "path")
  const cwd = textField(preview, "cwd")
  const command = textField(preview, "command")
  const diff = textField(preview, "diff")
  return (
    <div className="run-approval-preview">
      {path ? <div className="run-activity-path">{path}</div> : null}
      {cwd ? <div className="run-activity-path">In {cwd}</div> : null}
      {command ? <pre>{command}</pre> : null}
      {diff ? <pre>{diff}</pre> : null}
    </div>
  )
}

function argumentSummary(argumentsValue: Record<string, unknown> | null) {
  if (!argumentsValue) return null
  for (const key of ["path", "query", "operation", "command"]) {
    const value = textField(argumentsValue, key)
    if (value) return value
  }
  return null
}

export function RunActivity({
  label = "Run activity",
  events,
  runInfo,
  summary: savedSummary,
  summaryError,
  onRetrySummary,
  pending,
  approvalPendingToolId,
  approvalError,
  onApprovalDecision,
}: RunActivityProps) {
  const runId = runInfo?.runId ?? events.at(-1)?.runId ?? savedSummary?.run_id
  const summary = savedSummary?.run_id === runId ? savedSummary : null
  const [now, setNow] = useState(() => Date.now())
  const status = describeActivity(
    events,
    pending,
    now,
    undefined,
    summary ?? undefined
  )
  useEffect(() => {
    if (status.terminal) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [status.terminal])

  const ordered = [...events].sort(
    (left, right) => left.sequence - right.sequence
  )
  const groups = groupRunEvents(ordered)
  const toolNames = new Map<string, string>()
  for (const event of ordered) {
    if (event.eventType !== "tool.call") continue
    const id = textField(event.data, "tool_call_id")
    const name = textField(event.data, "name")
    if (id && name) toolNames.set(id, name)
  }
  const decided = new Set(
    ordered
      .filter((event) => event.eventType === "tool.approval_decided")
      .map((event) => textField(event.data, "tool_call_id"))
  )
  const totals = activityTotals(ordered, summary, runId)
  const approvalClosed = ordered.some((event) =>
    [
      "run.cancellation_requested",
      "run.completed",
      "run.failed",
      "run.cancelled",
      "run.interrupted",
    ].includes(event.eventType)
  )
  const modelLimit = summary?.max_model_calls ?? runInfo?.limits?.maxModelCalls
  const toolLimit = summary?.max_tool_calls ?? runInfo?.limits?.maxToolCalls
  const tokenLimit =
    summary?.max_total_tokens ?? runInfo?.limits?.maxTotalTokens
  const costLimit = summary?.max_cost_usd ?? runInfo?.limits?.maxCostUsd
  const latestResumedDeadline = ordered.findLast(
    (event) =>
      event.eventType === "run.resumed" &&
      typeof event.data.deadline_at === "string" &&
      Number.isFinite(Date.parse(event.data.deadline_at))
  )
  const deadlineAt = latestResumedDeadline
    ? (textField(latestResumedDeadline.data, "deadline_at") ?? undefined)
    : (summary?.deadline_at ?? runInfo?.limits?.deadlineAt)
  const deadline = deadlineAt ? new Date(deadlineAt) : null

  return (
    <section className="run-activity" aria-label={label}>
      <div className="run-activity-header">
        <span className="run-activity-kicker">Activity</span>
        <span className="run-activity-budget-name">
          {budgetLabel(summary?.budget_preset ?? runInfo?.budgetPreset)}
        </span>
      </div>
      <div className="run-activity-budget" aria-label="Run limits and usage">
        <span>{status.label}</span>
        <span>Elapsed {status.duration ?? "—"}</span>
        <span>
          {totals.modelCalls ?? "—"} / {modelLimit ?? "—"} model calls
        </span>
        <span>
          {totals.toolCalls ?? "—"} / {toolLimit ?? "—"} tools
        </span>
        <span>
          {totals.totalTokens === null
            ? "—"
            : numberFormat.format(totals.totalTokens)}{" "}
          / {tokenLimit === undefined ? "—" : numberFormat.format(tokenLimit)}{" "}
          tokens
          {totals.totalTokens !== null && !totals.usageComplete
            ? " (partial)"
            : ""}
        </span>
        <span>
          {totals.totalCost === null ? "—" : estimatedCost(totals.totalCost)} /{" "}
          {costLimit === undefined ? "—" : estimatedCost(costLimit)} estimated
          {totals.totalCost !== null && !totals.costComplete
            ? " (partial)"
            : ""}
        </span>
        {deadline && Number.isFinite(deadline.getTime()) ? (
          <time dateTime={deadlineAt}>
            Deadline{" "}
            {deadline.toLocaleString(undefined, {
              dateStyle: "medium",
              timeStyle: "short",
            })}
          </time>
        ) : null}
        {summary?.model_id ? <span>{summary.model_id}</span> : null}
        {runId ? <code>Run {runId}</code> : null}
      </div>
      {totals.updating ? (
        <p className="run-activity-load-state">Saved totals are updating…</p>
      ) : null}
      {summaryError ? (
        <p className="run-activity-load-state" role="alert">
          {summaryError}{" "}
          {onRetrySummary ? (
            <button type="button" onClick={onRetrySummary}>
              Retry totals
            </button>
          ) : null}
        </p>
      ) : !summary ? (
        <p className="run-activity-load-state">Run totals are loading…</p>
      ) : null}
      {summary?.error_message && !totals.updating ? (
        <p className="run-activity-load-state" role="status">
          {summary.error_message}
          {summary.error_code ? ` (${summary.error_code})` : ""}
        </p>
      ) : null}
      {!events.length ? (
        <p className="run-activity-load-state">
          {pending
            ? "Waiting for detailed activity…"
            : "Detailed activity is unavailable. Saved run details remain above."}
        </p>
      ) : null}
      <ol className="run-activity-list" aria-label="Run activity steps">
        {groups.map((group) => (
          <li className={`run-activity-group ${group.kind}`} key={group.key}>
            <h3 className="run-activity-group-title">{group.title}</h3>
            <ol className="run-activity-group-events">
              {group.events.map((event) => {
                const toolCallId = textField(event.data, "tool_call_id")
                const toolName =
                  textField(event.data, "name") ??
                  (toolCallId ? toolNames.get(toolCallId) : null) ??
                  "tool"
                let title: string
                let detail: React.ReactNode = null
                let tone = "normal"

                switch (event.eventType) {
                  case "run.queued":
                    title = "Run queued"
                    break
                  case "run.started":
                    title = "Run started"
                    break
                  case "run.resumed":
                    title = "Run resumed"
                    break
                  case "model.usage": {
                    title = "Model usage"
                    const input = numberField(event.data, "input_tokens")
                    const output = numberField(event.data, "output_tokens")
                    detail = (
                      <span>
                        {input === null ? "—" : numberFormat.format(input)}{" "}
                        input ·{" "}
                        {output === null ? "—" : numberFormat.format(output)}{" "}
                        output tokens
                      </span>
                    )
                    break
                  }
                  case "model.completed":
                    title = "Model call completed"
                    break
                  case "assistant.message": {
                    title = "Working note"
                    const content = textField(event.data, "content")
                    detail = content ? <p>{content}</p> : null
                    break
                  }
                  case "tool.call": {
                    title = `Tool requested · ${toolName}`
                    const summary = argumentSummary(
                      recordField(event.data, "arguments")
                    )
                    detail = summary ? <code>{summary}</code> : null
                    break
                  }
                  case "tool.approval_requested": {
                    title = "Approval needed"
                    tone = "approval"
                    const canAnswer =
                      pending &&
                      !approvalClosed &&
                      !!toolCallId &&
                      !decided.has(toolCallId)
                    const preview = recordField(event.data, "preview")
                    const requestArguments = recordField(
                      event.data,
                      "arguments"
                    )
                    detail = (
                      <div
                        className="run-approval"
                        role="region"
                        aria-label={`Approval required for ${toolName}`}
                      >
                        <div className="run-approval-tool">{toolName}</div>
                        {previewDetails(preview)}
                        {requestArguments ? (
                          <details
                            className="run-approval-arguments"
                            open={!preview}
                          >
                            <summary>Request arguments</summary>
                            <pre>
                              {JSON.stringify(requestArguments, null, 2)}
                            </pre>
                          </details>
                        ) : null}
                        {canAnswer ? (
                          <div className="run-approval-actions">
                            <button
                              type="button"
                              onClick={() =>
                                onApprovalDecision(toolCallId, "approved")
                              }
                              disabled={approvalPendingToolId === toolCallId}
                            >
                              Approve
                            </button>
                            <button
                              type="button"
                              onClick={() =>
                                onApprovalDecision(toolCallId, "denied")
                              }
                              disabled={approvalPendingToolId === toolCallId}
                            >
                              Deny
                            </button>
                          </div>
                        ) : null}
                        {canAnswer && approvalError ? (
                          <p role="alert">{approvalError}</p>
                        ) : null}
                      </div>
                    )
                    break
                  }
                  case "tool.approval_decided":
                    title =
                      textField(event.data, "decision") === "approved"
                        ? "Approved"
                        : "Denied"
                    tone = title === "Denied" ? "warning" : "normal"
                    detail = <span>{toolName}</span>
                    break
                  case "tool.result": {
                    const status = textField(event.data, "status")
                    title =
                      status === "denied"
                        ? "Tool denied"
                        : status === "completed"
                          ? "Tool completed"
                          : status === "cancelled"
                            ? "Tool cancelled"
                            : "Tool failed"
                    tone = status === "completed" ? "normal" : "warning"
                    const content = textField(event.data, "content")
                    detail = content ? (
                      <pre className="run-tool-result">{content}</pre>
                    ) : null
                    break
                  }
                  case "run.cancellation_requested":
                    title = "Stopping run"
                    break
                  case "run.completed":
                    title = "Run completed"
                    break
                  case "run.cancelled":
                    title = "Run cancelled"
                    tone = "warning"
                    break
                  case "run.interrupted":
                    title = "Run interrupted"
                    tone = "warning"
                    break
                  case "run.failed": {
                    title = failureTitle(textField(event.data, "code"))
                    tone = "warning"
                    const message = textField(event.data, "message")
                    detail = message ? <p>{message}</p> : null
                    break
                  }
                  default:
                    return null
                }

                return (
                  <li
                    className={`run-activity-entry ${tone}`}
                    data-run-sequence={event.sequence}
                    key={event.sequence}
                  >
                    <span className="run-activity-marker" aria-hidden="true" />
                    <div className="run-activity-entry-copy">
                      <div className="run-activity-entry-heading">
                        <div className="run-activity-title">{title}</div>
                        <RunEventTimestamp createdAt={event.createdAt} />
                      </div>
                      {detail ? (
                        <div className="run-activity-detail">{detail}</div>
                      ) : null}
                    </div>
                  </li>
                )
              })}
            </ol>
          </li>
        ))}
      </ol>
      <RunEventLog events={ordered} />
    </section>
  )
}
