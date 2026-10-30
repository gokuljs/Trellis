import type { ResearchSnapshot } from "@/lib/app-types"
import { researchPhaseLabel } from "@/lib/research"
import "./research.css"

export function ResearchProgress({ research }: { research: ResearchSnapshot }) {
  return (
    <section className="research-progress" aria-label="Research progress">
      <div className="research-progress-heading">
        <span>{researchPhaseLabel(research.phase)}</span>
        {research.remaining_seconds !== null ? (
          <span>
            {Math.ceil(Math.max(0, research.remaining_seconds) / 60)} min
            remaining
            {research.pending_clarification ? " · Timer paused" : ""}
          </span>
        ) : null}
      </div>
      {research.subquestions.length ? (
        <ul className="research-questions">
          {research.subquestions.map((question) => (
            <li key={question.id}>
              <span>{question.question}</span>
              <span className="research-question-status">
                {question.status.replaceAll("_", " ")}
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
