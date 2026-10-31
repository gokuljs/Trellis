from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.dependencies import AccountDep
from app.application.errors import ApplicationError
from app.application.research_notebook import notebook_coverage
from app.application.research_task_context import parse_research_read_observations
from app.domain.research import RESEARCH_POLICY_V2, ResearchPhase, ResearchSnapshot, research_limits
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


class ResearchBriefResponse(BaseModel):
    scope: str
    comparison_dimensions: list[str] = Field(default_factory=list)
    source_restrictions: list[str] = Field(default_factory=list)
    expected_output: str = ""


class ResearchRequirementResponse(BaseModel):
    id: str
    status: str
    finding_ids: list[str]
    description: str = ""
    depth: str = "source_excerpt"


class ResearchCoverageResponse(BaseModel):
    id: str
    status: str
    requirements: list[ResearchRequirementResponse]


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
    policy_version: str | None = None
    execution_budget_seconds: int | None = None
    brief: ResearchBriefResponse | None = None
    coverage: list[ResearchCoverageResponse] = Field(default_factory=list)
    evidence_depth: list[str] = Field(default_factory=list)
    sources_read: int | None = None
    source_windows_read: int | None = None


router = APIRouter(prefix="/api/sessions", tags=["research"])


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _records(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _issues(state: ResearchSnapshot, key: str) -> list[str]:
    if state.policy_version != RESEARCH_POLICY_V2:
        return _strings(state.checkpoint.get(key))
    issues: dict[str, dict[str, object]] = {}
    legacy: list[str] = []
    for checkpoint in [
        *(task.notebook for task in state.tasks if task.role == "worker"),
        state.checkpoint,
    ]:
        records = checkpoint.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, str):
                legacy.append(record)
            elif isinstance(record, dict) and isinstance(record.get("id"), str):
                issues[record["id"]] = record
    return list(
        dict.fromkeys(
            [
                *legacy,
                *(
                    str(issue["text"])
                    for issue in issues.values()
                    if issue.get("status") != "resolved" and isinstance(issue.get("text"), str)
                ),
            ]
        )
    )


def _coverage(state: ResearchSnapshot, published: bool) -> list[ResearchCoverageResponse]:
    if state.policy_version != RESEARCH_POLICY_V2:
        return []
    tasks = [task.notebook for task in state.tasks if task.role == "worker"]
    coverage = notebook_coverage(state.checkpoint, tasks)
    descriptions = {}
    for question in _records(state.checkpoint.get("subquestions")):
        for requirement in _records(question.get("evidence_requirements")):
            if isinstance(requirement.get("id"), str):
                descriptions[requirement["id"]] = requirement
    reviewed: dict[str, set[str]] = {}
    if published:
        for item in state.reviewed_coverage:
            question_id = item.get("question_id")
            if isinstance(question_id, str) and _strings(item.get("claim_ids")):
                reviewed.setdefault(question_id, set()).update(
                    _strings(item.get("requirement_ids"))
                )
    result = []
    for question in coverage:
        requirements = []
        question_id = str(question["id"])
        for item in _records(question.get("requirements")):
            identity = str(item["id"])
            metadata = descriptions.get(identity, {})
            requirements.append(
                ResearchRequirementResponse(
                    id=identity,
                    status="reviewed"
                    if identity in reviewed.get(question_id, set())
                    else str(item["status"]),
                    finding_ids=_strings(item.get("finding_ids")),
                    description=str(metadata.get("description", "")),
                    depth=str(metadata.get("depth", "source_excerpt")),
                )
            )
        status = (
            "reviewed"
            if requirements and all(item.status == "reviewed" for item in requirements)
            else "evidence_recorded"
            if requirements and all(item.status != "missing" for item in requirements)
            else "missing"
        )
        result.append(
            ResearchCoverageResponse(id=question_id, status=status, requirements=requirements)
        )
    return result


def _read_progress(state: ResearchSnapshot) -> tuple[int | None, int | None, list[str]]:
    if state.policy_version != RESEARCH_POLICY_V2:
        return None, None, []
    tasks = {task.id for task in state.tasks if task.role in {"lead", "worker"}}
    observations = parse_research_read_observations(
        [item for item in state.source_reads if item.get("task_id") in tasks]
    )
    sources = {observation.source_id for observation in observations}
    windows = {
        (observation.tool_call_id, window.start_line, window.end_line)
        for observation in observations
        for window in observation.presented_ranges
    }
    depths = set()
    for checkpoint in [state.checkpoint, *(task.notebook for task in state.tasks)]:
        findings = checkpoint.get("findings")
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get("citations"), list):
                continue
            if any(
                ref.get("source_id") == observation.source_id
                and isinstance(start := ref.get("start_line"), int)
                and isinstance(end := ref.get("end_line"), int)
                and not isinstance(start, bool)
                and not isinstance(end, bool)
                and any(
                    start <= window.end_line and end >= window.start_line
                    for window in observation.presented_ranges
                )
                for ref in _records(finding.get("citations"))
                for observation in observations
            ):
                depth = finding.get("evidence_depth")
                if depth in {"source_excerpt", "abstract", "full_text", "listing"}:
                    depths.add(depth)
    if sources and not depths:
        depths.add("source_excerpt")
    return len(sources), len(windows), sorted(depths)


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
    raw_brief = state.checkpoint.get("brief")
    brief = None
    if isinstance(raw_brief, dict) and isinstance(raw_brief.get("scope"), str):
        brief = ResearchBriefResponse(
            scope=raw_brief["scope"],
            comparison_dimensions=_strings(raw_brief.get("comparison_dimensions")),
            source_restrictions=_strings(raw_brief.get("source_restrictions")),
            expected_output=str(raw_brief.get("expected_output", "")),
        )
    sources_read, source_windows_read, evidence_depth = _read_progress(state)
    coverage = _coverage(
        state,
        run.status is RunStatus.COMPLETED
        and state.report_model_call_id is not None
        and state.phase in {ResearchPhase.COMPLETED, ResearchPhase.PARTIAL},
    )
    if state.policy_version == RESEARCH_POLICY_V2:
        by_question = {item.id: item.status for item in coverage}
        questions = [
            question.model_copy(
                update={
                    "status": "addressed"
                    if by_question.get(question.id) == "reviewed"
                    else "evidence_recorded"
                    if by_question.get(question.id) == "evidence_recorded"
                    else "unavailable"
                    if question.status == "unavailable"
                    else "open"
                }
            )
            for question in questions
        ]
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
        gaps=_issues(state, "gaps"),
        conflicts=_issues(state, "conflicts"),
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
        policy_version=state.policy_version,
        execution_budget_seconds=state.execution_budget_seconds
        or run.execution_budget_seconds
        or research_limits(run.budget_preset, run.research_policy_version).seconds,
        brief=brief,
        coverage=coverage,
        evidence_depth=evidence_depth,
        sources_read=sources_read,
        source_windows_read=source_windows_read,
    )
