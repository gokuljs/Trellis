import { useId, useState } from "react"

import type {
  LatestRun,
  UncertainOperation,
  WorkspaceReviewStatus,
} from "@/lib/app-types"
import { ApiError } from "@/lib/api"
import "./workspace-review.css"

type ReviewSubmission =
  | { status: "idle" }
  | { status: "submitting" }
  | { status: "failed"; message: string }

type WorkspaceReviewProps = {
  operation: UncertainOperation
  runId: string
  sourceSessionId?: string
  workspacePath?: string | null
  onReview: (
    runId: string,
    toolCallId: string,
    sourceSessionId?: string
  ) => Promise<void>
}

function WorkspaceReview({
  operation,
  runId,
  sourceSessionId,
  workspacePath,
  onReview,
}: WorkspaceReviewProps) {
  const confirmationId = useId()
  const [confirmed, setConfirmed] = useState(false)
  const [submission, setSubmission] = useState<ReviewSubmission>({
    status: "idle",
  })
  const submitting = submission.status === "submitting"

  return (
    <section
      className="workspace-review run-approval"
      aria-label="Review interrupted command"
    >
      <h2>Command outcome unknown</h2>
      <p>
        Check this command and the workspace before allowing automatic writes
        again.
      </p>
      <div className="run-approval-preview">
        {workspacePath ? (
          <div className="run-activity-path">Folder {workspacePath}</div>
        ) : null}
        <div className="run-approval-tool">{operation.name}</div>
        <pre>{JSON.stringify(operation.arguments, null, 2)}</pre>
      </div>
      <p className="run-activity-path">Operation {operation.tool_call_id}</p>
      {operation.reviewed_at && operation.review_pending === false ? (
        <p role="status">
          Review acknowledged. The matching workspace block is cleared. This run
          still has an unknown outcome and cannot be retried.
        </p>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            if (!confirmed || submitting) return
            setSubmission({ status: "submitting" })
            void onReview(runId, operation.tool_call_id, sourceSessionId)
              .then(() => setSubmission({ status: "idle" }))
              .catch((error: unknown) => {
                setSubmission({
                  status: "failed",
                  message:
                    error instanceof ApiError
                      ? error.message
                      : "Trellis could not record this review. Try again.",
                })
              })
          }}
        >
          <label
            className="workspace-review-confirmation"
            htmlFor={confirmationId}
          >
            <input
              id={confirmationId}
              type="checkbox"
              checked={confirmed}
              disabled={submitting}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>I reviewed this command and checked the workspace.</span>
          </label>
          <p>
            Acknowledging does not repeat the command or confirm that it
            succeeded. Retry remains unavailable for this run.
          </p>
          <div className="run-approval-actions">
            <button type="submit" disabled={!confirmed || submitting}>
              {submitting ? "Recording review…" : "Acknowledge review"}
            </button>
          </div>
          {submission.status === "failed" ? (
            <p role="alert">{submission.message}</p>
          ) : null}
        </form>
      )}
    </section>
  )
}

export function WorkspaceRecoveryNotice({
  run,
  workspace = null,
  onReview,
  onRefresh = () => undefined,
}: {
  run: LatestRun | null
  workspace?: WorkspaceReviewStatus | null
  onReview: (
    runId: string,
    toolCallId: string,
    sourceSessionId?: string
  ) => Promise<void>
  onRefresh?: () => void
}) {
  const workspaceOperation = workspace?.uncertain_operation
  const operation =
    workspace?.blocked && !workspaceOperation
      ? null
      : (workspaceOperation ?? run?.uncertain_operation)
  const operationRunId = workspaceOperation ? workspace?.run_id : run?.run_id
  const sourceSessionId = workspaceOperation
    ? workspace?.source_session_id
    : undefined
  const blockedReasons: Record<string, string> = {
    workspace_review_other_account:
      "This folder has an unresolved command from another account. Sign in to that account to review it.",
    workspace_review_run_active:
      "A command is still active in this folder. Automatic writes remain blocked until its run has stopped.",
    workspace_review_unavailable:
      "Trellis could not check this folder's unresolved commands. Check the workspace again before continuing automatic writes.",
  }
  return (
    <>
      {run?.recovery_reason || run?.retry_allowed === false ? (
        <div className="workspace-recovery-reason" role="status">
          {run.recovery_reason ? (
            <p>Recovery: {run.recovery_reason.replaceAll("_", " ")}</p>
          ) : null}
          {run.retry_allowed === false &&
          run.uncertain_operation?.name !== "run_command" ? (
            <p>
              Retry is unavailable because an operation's outcome could not be
              verified.
            </p>
          ) : null}
        </div>
      ) : null}
      {workspace?.blocked && !workspaceOperation ? (
        <div className="workspace-recovery-reason" role="status">
          <p>
            {blockedReasons[workspace.reason ?? ""] ??
              "This folder has an unresolved command. Check its workspace review before continuing automatic writes."}
          </p>
          <button type="button" className="settings-save" onClick={onRefresh}>
            Check workspace
          </button>
        </div>
      ) : null}
      {operation?.name === "run_command" && operationRunId ? (
        <WorkspaceReview
          key={`${operationRunId}:${operation.tool_call_id}`}
          runId={operationRunId}
          sourceSessionId={sourceSessionId}
          workspacePath={workspaceOperation ? workspace?.workspace_path : null}
          operation={operation}
          onReview={onReview}
        />
      ) : null}
    </>
  )
}
