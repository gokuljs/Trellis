import type { ResearchSnapshot } from "@/lib/app-types"
import {
  researchEvidenceDepthLabel,
  researchPhaseLabel,
  researchQuestionEvidenceLabel,
} from "@/lib/research"
import "./research.css"

export function ResearchProgress({ research }: { research: ResearchSnapshot }) {
  const evidenceDepth = researchEvidenceDepthLabel(research.evidence_depth)
  return (
    <section className="research-progress" aria-label="Research progress">
      <div className="research-progress-heading">
        <span>{researchPhaseLabel(research.phase)}</span>
        {research.remaining_seconds !== null ? (
          <span>
            {Math.ceil(Math.max(0, research.remaining_seconds) / 60)} min
            remaining
            {research.pending_clarification ? " · Timer paused" : ""}
            {research.execution_budget_seconds ? (
              <>
                {" "}
                · {Math.round(research.execution_budget_seconds / 60)} min
                allowance
              </>
            ) : null}
          </span>
        ) : null}
      </div>
      {typeof research.sources_read === "number" || evidenceDepth ? (
        <p className="research-evidence-summary">
          {typeof research.sources_read === "number" ? (
            <span>
              {research.sources_read}{" "}
              {research.sources_read === 1 ? "source" : "sources"} read
              {typeof research.source_windows_read === "number"
                ? ` · ${research.source_windows_read} reading ${research.source_windows_read === 1 ? "window" : "windows"}`
                : ""}
            </span>
          ) : null}
          {evidenceDepth ? <span>{evidenceDepth}</span> : null}
        </p>
      ) : null}
      {research.brief ? (
        <details className="research-scope">
          <summary>Research scope</summary>
          <p>{research.brief.scope}</p>
          {research.brief.comparison_dimensions.length ? (
            <p>Comparing: {research.brief.comparison_dimensions.join(", ")}</p>
          ) : null}
          {research.brief.source_restrictions.length ? (
            <p>Sources: {research.brief.source_restrictions.join("; ")}</p>
          ) : null}
        </details>
      ) : null}
      {research.subquestions.length ? (
        <ul className="research-questions">
          {research.subquestions.map((question) => (
            <li key={question.id}>
              <span>{question.question}</span>
              <span className="research-question-status">
                {researchQuestionEvidenceLabel(
                  research,
                  question.id,
                  question.status
                )}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {research.tasks.length ? (
        <details className="research-workers">
          <summary>
            {research.tasks.filter((task) => task.status === "running").length}{" "}
            active researchers · {research.tasks.length} assignments
          </summary>
          <ul className="research-questions">
            {research.tasks.map((task) => (
              <li key={task.id}>
                <span>{task.objective}</span>
                <span className="research-question-status">
                  {task.status.replaceAll("_", " ")}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {research.conflicts.length ? (
        <div>
          <p className="research-detail-label">Conflicting evidence</p>
          <ul>
            {research.conflicts.map((conflict, index) => (
              <li key={index}>{conflict}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {research.gaps.length ? (
        <div>
          <p className="research-detail-label">Evidence still missing</p>
          <ul>
            {research.gaps.map((gap, index) => (
              <li key={index}>{gap}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}
