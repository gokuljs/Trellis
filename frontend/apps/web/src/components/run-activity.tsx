import type {
  RuntimeRunEvent,
  RuntimeRunInfo,
  ToolApprovalDecision,
} from "@/lib/runtime-client"

type RunActivityProps = {
  events: RuntimeRunEvent[]
  runInfo: RuntimeRunInfo | null
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
  if (preset === "conservative") return "Conservative budget"
  if (preset === "longer") return "Longer coding run budget"
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
  events,
  runInfo,
  pending,
  approvalPendingToolId,
  approvalError,
  onApprovalDecision,
}: RunActivityProps) {
  if (events.length === 0) return null

  const ordered = [...events].sort(
    (left, right) => left.sequence - right.sequence
  )
  const visible = ordered.filter(
    (event) =>
      !["assistant.delta", "assistant.completed"].includes(event.eventType)
  )
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
  const modelCalls = ordered.filter(
    (event) => event.eventType === "model.completed"
  ).length
  const toolCalls = ordered.filter(
    (event) => event.eventType === "tool.call"
  ).length
  const latestUsage = ordered.findLast(
    (event) => event.eventType === "model.usage"
  )
  const totalTokens = latestUsage
    ? numberField(latestUsage.data, "total_tokens")
    : null
  const totalCost = latestUsage
    ? numberField(latestUsage.data, "total_estimated_cost_usd")
    : null
  const approvalClosed = ordered.some((event) =>
    [
      "run.cancellation_requested",
      "run.completed",
      "run.failed",
      "run.cancelled",
      "run.interrupted",
    ].includes(event.eventType)
  )
  const modelLimit = runInfo?.limits?.maxModelCalls
  const toolLimit = runInfo?.limits?.maxToolCalls
  const tokenLimit = runInfo?.limits?.maxTotalTokens
  const costLimit = runInfo?.limits?.maxCostUsd
  const latestResumedDeadline = ordered.findLast(
    (event) =>
      event.eventType === "run.resumed" &&
      typeof event.data.deadline_at === "string" &&
      Number.isFinite(Date.parse(event.data.deadline_at))
  )
  const deadlineAt = latestResumedDeadline
    ? (textField(latestResumedDeadline.data, "deadline_at") ?? undefined)
    : runInfo?.limits?.deadlineAt
  const deadline = deadlineAt ? new Date(deadlineAt) : null

  return (
    <section className="run-activity" aria-label="Run activity">
      <div className="run-activity-header">
        <span className="run-activity-kicker">Activity</span>
        <span className="run-activity-budget-name">
          {budgetLabel(runInfo?.budgetPreset)}
        </span>
      </div>
      <div className="run-activity-budget" aria-label="Run limits and usage">
        <span>
          {modelCalls} / {modelLimit ?? "—"} model calls
        </span>
        <span>
          {toolCalls} / {toolLimit ?? "—"} tools
        </span>
        <span>
          {totalTokens === null ? "—" : numberFormat.format(totalTokens)} /{" "}
          {tokenLimit === undefined ? "—" : numberFormat.format(tokenLimit)}{" "}
          tokens
        </span>
        <span>
          {totalCost === null ? "—" : estimatedCost(totalCost)} /{" "}
          {costLimit === undefined ? "—" : estimatedCost(costLimit)} estimated
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
      </div>
      <ol className="run-activity-list" aria-label="Run activity timeline">
        {visible.map((event) => {
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
                  {input === null ? "—" : numberFormat.format(input)} input ·{" "}
                  {output === null ? "—" : numberFormat.format(output)} output
                  tokens
                </span>
              )
              break
            }
            case "model.completed":
              title = `Model step ${ordered.filter((item) => item.eventType === "model.completed" && item.sequence <= event.sequence).length} complete`
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
              const requestArguments = recordField(event.data, "arguments")
              detail = (
                <div
                  className="run-approval"
                  role="region"
                  aria-label={`Approval required for ${toolName}`}
                >
                  <div className="run-approval-tool">{toolName}</div>
                  {previewDetails(preview)}
                  {requestArguments ? (
                    <details className="run-approval-arguments" open={!preview}>
                      <summary>Request arguments</summary>
                      <pre>{JSON.stringify(requestArguments, null, 2)}</pre>
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
                        onClick={() => onApprovalDecision(toolCallId, "denied")}
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
                <div className="run-activity-title">{title}</div>
                {detail ? (
                  <div className="run-activity-detail">{detail}</div>
                ) : null}
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
