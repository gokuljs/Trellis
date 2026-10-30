from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import AccountDep
from app.application.errors import ApplicationError
from app.domain.runtime import RunStatus, is_terminal_run_status


class ResearchQuestionResponse(BaseModel):
    id: str
    question: str
    status: str


class ResearchTaskResponse(BaseModel):
    id: str
    objective: str
    status: str


class ResearchClarificationResponse(BaseModel):
    id: str
    question: str


class ResearchCitationResponse(BaseModel):
    source_id: str
    start_line: int
    end_line: int


class ResearchResponse(BaseModel):
    run_id: str
    phase: str
    objective: str
    subquestions: list[ResearchQuestionResponse]
    tasks: list[ResearchTaskResponse]
    gaps: list[str]
    conflicts: list[str]
    pending_clarification: ResearchClarificationResponse | None
    stop_reason: str | None
    remaining_seconds: float
    citations: list[ResearchCitationResponse]
    report_model_call_id: str | None


router = APIRouter(prefix="/api/sessions", tags=["research"])


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


@router.get("/{session_id}/runs/{run_id}/research")
async def get_research(session_id: UUID, run_id: UUID, account: AccountDep) -> ResearchResponse:
    run = await account.run_service.get_run(str(run_id))
    if run is None or run.session_id != str(session_id):
        raise ApplicationError("run_not_found", "Run not found.")
    state = await account.database.read_research(str(run_id))
    if state is None or state.session_id != str(session_id):
        raise ApplicationError("research_not_found", "Research not found.")
    closed_status = (
        "cancelled" if run.status in {RunStatus.CANCELLED, RunStatus.COMPLETED} else "failed"
    )
    questions = []
    raw_questions = state.checkpoint.get("subquestions", [])
    if isinstance(raw_questions, list):
        for raw in raw_questions:
            if (
                isinstance(raw, dict)
                and isinstance(raw.get("id"), str)
                and isinstance(raw.get("question"), str)
            ):
                questions.append(
                    ResearchQuestionResponse(
                        id=raw["id"],
                        question=raw["question"],
                        status=str(raw.get("status", "pending")),
                    )
                )
    return ResearchResponse(
        run_id=state.run_id,
        phase=state.phase.value,
        objective=state.objective,
        subquestions=questions,
        tasks=[
            ResearchTaskResponse(
                id=task.id,
                objective=task.objective,
                status=closed_status
                if is_terminal_run_status(run.status) and task.status.value in {"queued", "running"}
                else task.status.value,
            )
            for task in state.tasks
            if task.role == "worker"
        ],
        gaps=_strings(state.checkpoint.get("gaps")),
        conflicts=_strings(state.checkpoint.get("conflicts")),
        pending_clarification=ResearchClarificationResponse.model_validate(
            state.pending_clarification
        )
        if state.pending_clarification is not None
        else None,
        stop_reason=state.stop_reason,
        remaining_seconds=state.remaining_seconds,
        citations=[
            ResearchCitationResponse.model_validate(citation, from_attributes=True)
            for citation in state.citations
        ],
        report_model_call_id=state.report_model_call_id,
    )
