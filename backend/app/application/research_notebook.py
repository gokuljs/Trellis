import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Literal, TypedDict, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.application.research_task_context import (
    ResearchReadObservation,
    parse_research_read_observations,
)
from app.domain.research import ResearchSnapshot, research_limits, research_object
from app.domain.runtime import RunSnapshot

EvidenceDepth = Literal["source_excerpt", "abstract", "full_text", "listing"]
BriefEntry = Annotated[str, Field(min_length=1, max_length=2000)]


class ResearchCollectionGuidance(TypedDict):
    coverage: list[dict[str, object]]
    unresolved_requirements: list[dict[str, object]]
    unassigned_requirement_ids: list[str]
    unassigned_question_ids: list[str]
    unanswered_question_ids: list[str]
    unavailable_requirements: list[dict[str, object]]
    open_gaps: list[dict[str, object]]
    open_conflicts: list[dict[str, object]]
    examined_source_ids: list[str]
    remaining_collection_rounds: int
    remaining_worker_tasks: int
    collection_seconds_remaining: float
    next_action: str
    reason: str
    requires_report_review: bool
    shared_budget: dict[str, object]


class NotebookArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceReference(NotebookArguments):
    source_id: str = Field(min_length=36, max_length=36)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class EvidenceRequirement(NotebookArguments):
    id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    depth: EvidenceDepth = "source_excerpt"


class ResearchQuestion(NotebookArguments):
    id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    status: Literal["open", "addressed", "unavailable"] = "open"
    evidence_requirements: list[EvidenceRequirement] = Field(min_length=1, max_length=12)


class SourcedFinding(NotebookArguments):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=8000)
    question_ids: list[str] = Field(min_length=1, max_length=12)
    requirement_ids: list[str] = Field(min_length=1, max_length=24)
    citations: list[EvidenceReference] = Field(min_length=1, max_length=10)
    support_kind: Literal["author_reported", "corroborated", "inference"] = "author_reported"
    evidence_depth: EvidenceDepth = "source_excerpt"


class ResearchIssue(NotebookArguments):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)
    question_ids: list[str] = Field(min_length=1, max_length=12)
    requirement_ids: list[str] = Field(default_factory=list, max_length=24)
    status: Literal["open", "resolved", "unavailable"] = "open"
    citations: list[EvidenceReference] = Field(default_factory=list, max_length=10)


class ResearchBrief(NotebookArguments):
    scope: str = Field(min_length=1, max_length=8000)
    comparison_dimensions: list[BriefEntry] = Field(default_factory=list, max_length=12)
    source_restrictions: list[BriefEntry] = Field(default_factory=list, max_length=12)
    expected_output: str = Field(default="", max_length=2000)


class ResearchCollectionDecision(NotebookArguments):
    reason: Literal["sufficient", "low_expected_value", "evidence_unavailable"]
    explanation: str = Field(min_length=1, max_length=2000)


class WorkerCheckpointV2(NotebookArguments):
    phase: Literal["scoping", "researching", "checking_conflict"] = "researching"
    findings: list[SourcedFinding] = Field(default_factory=list, max_length=50)
    examined_source_ids: list[str] = Field(default_factory=list, max_length=40)
    gaps: list[ResearchIssue] = Field(default_factory=list, max_length=50)
    conflicts: list[ResearchIssue] = Field(default_factory=list, max_length=50)
    stop_reason: str | None = Field(default=None, max_length=100)


class ResearchCheckpointV2(WorkerCheckpointV2):
    scope: str = Field(default="", max_length=8000)
    brief: ResearchBrief | None = None
    subquestions: list[ResearchQuestion] = Field(default_factory=list, max_length=12)
    collection_decision: ResearchCollectionDecision | None = None


def _records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, object], item) for item in value if isinstance(item, dict)]


def _by_id(records: Iterable[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["id"]): item for item in records if isinstance(item.get("id"), str)}


def merge_notebook(
    previous: dict[str, object],
    update: dict[str, object],
    *,
    known_questions: object = None,
) -> dict[str, object]:
    checked = ResearchCheckpointV2.model_validate(update).model_dump()
    merged = dict(previous)
    for key in ("phase", "scope", "stop_reason", "brief", "collection_decision"):
        if checked[key]:
            merged[key] = checked[key]
    for key in ("subquestions", "findings", "gaps", "conflicts"):
        merged[key] = list(_by_id([*_records(previous.get(key)), *_records(checked[key])]).values())
    sources = [
        *cast(list[str], previous.get("examined_source_ids", [])),
        *checked["examined_source_ids"],
    ]
    merged["examined_source_ids"] = list(dict.fromkeys(sources))
    for source in sources:
        UUID(source)

    questions = _by_id([*_records(known_questions), *_records(merged.get("subquestions"))])
    requirements: dict[str, str] = {}
    for question in questions.values():
        for requirement in _records(question.get("evidence_requirements")):
            identity = str(requirement["id"])
            if identity in requirements:
                raise ValueError("research requirement identifiers must be unique")
            requirements[identity] = str(question["id"])
    for key in ("findings", "gaps", "conflicts"):
        for item in _records(merged[key]):
            question_ids = cast(list[str], item["question_ids"])
            if not set(question_ids) <= questions.keys():
                raise ValueError("research evidence refers to an unknown question")
            if any(
                requirements.get(identity) not in question_ids
                for identity in cast(list[str], item.get("requirement_ids", []))
            ):
                raise ValueError("research evidence refers to an unknown requirement")
            for reference in _records(item.get("citations")):
                UUID(str(reference["source_id"]))
                if cast(int, reference["end_line"]) < cast(int, reference["start_line"]):
                    raise ValueError("citation range is reversed")
    ResearchCheckpointV2.model_validate(
        {key: value for key, value in merged.items() if key in ResearchCheckpointV2.model_fields}
    )
    return research_object(merged)


def notebook_coverage(
    checkpoint: dict[str, object], task_checkpoints: Iterable[dict[str, object]] = ()
) -> list[dict[str, object]]:
    """Describe recorded evidence; source access and semantic support still need review."""
    checkpoints = [checkpoint, *task_checkpoints]
    questions = _by_id(
        [
            *(
                question
                for item in checkpoints[1:]
                for question in _records(item.get("subquestions"))
            ),
            *_records(checkpoint.get("subquestions")),
        ]
    )
    findings = [finding for item in checkpoints for finding in _records(item.get("findings"))]
    coverage: list[dict[str, object]] = []
    for question in questions.values():
        requirements = []
        for requirement in _records(question.get("evidence_requirements")):
            supporting = [
                finding["id"]
                for finding in findings
                if question["id"] in cast(list[str], finding.get("question_ids", []))
                and requirement["id"] in cast(list[str], finding.get("requirement_ids", []))
                and finding.get("citations")
            ]
            requirements.append(
                {
                    "id": requirement["id"],
                    "status": "evidence_recorded" if supporting else "missing",
                    "finding_ids": supporting,
                }
            )
        coverage.append(
            {
                "id": question["id"],
                "status": "evidence_recorded"
                if requirements
                and all(item["status"] == "evidence_recorded" for item in requirements)
                else "missing",
                "requirements": requirements,
            }
        )
    return coverage


def collection_basis(
    checkpoint: dict[str, object], task_checkpoints: Iterable[dict[str, object]] = ()
) -> str:
    keys = (
        "brief",
        "scope",
        "subquestions",
        "findings",
        "gaps",
        "conflicts",
        "clarifications",
    )

    def selected(value: dict[str, object]) -> dict[str, object]:
        return {key: value[key] for key in keys if key in value}

    worker_states = sorted(
        json.dumps(selected(item), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        for item in task_checkpoints
    )
    payload = json.dumps(
        {"root": selected(checkpoint), "workers": worker_states},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _presented_finding(
    finding: dict[str, object], observations: Sequence[ResearchReadObservation]
) -> bool:
    for reference in _records(finding.get("citations")):
        start, end = reference.get("start_line"), reference.get("end_line")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            return False
        intervals = sorted(
            (item.start_line, item.end_line)
            for observation in observations
            if observation.source_id == reference.get("source_id")
            for item in observation.presented_ranges
        )
        through = start - 1
        for first, last in intervals:
            if first > through + 1:
                break
            through = max(through, last)
        if through < end:
            return False
    return bool(finding.get("citations"))


def collection_guidance(
    snapshot: ResearchSnapshot,
    run: RunSnapshot,
    *,
    shared_budget: Mapping[str, object] | None = None,
    observed_reads: Sequence[ResearchReadObservation] | None = (),
) -> ResearchCollectionGuidance:
    if observed_reads is not None and not observed_reads:
        observed_reads = parse_research_read_observations(snapshot.source_reads)
    worker_tasks = [item for item in snapshot.tasks if item.role == "worker"]
    worker_notes = [item.notebook for item in worker_tasks]
    checkpoints = [*worker_notes, snapshot.checkpoint]
    questions = _records(snapshot.checkpoint.get("subquestions"))
    findings = [item for note in checkpoints for item in _records(note.get("findings"))]
    gaps = _by_id(item for note in checkpoints for item in _records(note.get("gaps")))
    conflicts = _by_id(item for note in checkpoints for item in _records(note.get("conflicts")))
    open_gaps = [item for item in gaps.values() if item.get("status", "open") == "open"]
    open_conflicts = [item for item in conflicts.values() if item.get("status", "open") == "open"]
    unavailable_ids = {
        identity
        for item in gaps.values()
        if item.get("status") == "unavailable"
        for identity in cast(list[str], item.get("requirement_ids", []))
    }
    coverage = notebook_coverage(snapshot.checkpoint, worker_notes)
    requirements = {
        str(requirement["id"]): {**requirement, "question_id": question["id"]}
        for question in questions
        for requirement in _records(question.get("evidence_requirements"))
    }
    assigned_requirements = {
        str(identity)
        for note in worker_notes
        for key in ("findings", "gaps", "conflicts")
        for item in _records(note.get(key))
        for identity in cast(list[str], item.get("requirement_ids", []))
    }
    for task in worker_tasks:
        _, marker, evidence = task.objective.rpartition("\nEvidence required:\n")
        if not marker:
            continue
        evidence = "\n" + evidence.partition("\nConflicts to investigate:")[0] + "\n"
        for identity, requirement in requirements.items():
            entry = (
                f"\n- {identity} (Question: {requirement['question_id']}; "
                f"Depth: {requirement['depth']}): {requirement['description']}\n"
            )
            if entry in evidence:
                assigned_requirements.add(identity)
    unresolved, unavailable = [], []
    evidenced_questions: set[str] = set()
    for covered_question in coverage:
        for facet in _records(covered_question.get("requirements")):
            requirement = requirements.get(str(facet["id"]))
            if requirement is None:
                continue
            supporting = [
                finding
                for finding in findings
                if covered_question["id"] in cast(list[str], finding.get("question_ids", []))
                and facet["id"] in cast(list[str], finding.get("requirement_ids", []))
            ]
            enough = any(
                (
                    requirement.get("depth") == "source_excerpt"
                    or item.get("evidence_depth") == requirement.get("depth")
                )
                and (observed_reads is None or _presented_finding(item, observed_reads))
                for item in supporting
            )
            if enough:
                evidenced_questions.add(str(covered_question["id"]))
                continue
            if facet["id"] in unavailable_ids:
                facet["status"] = "unavailable"
                unavailable.append({**requirement, "status": "unavailable"})
            else:
                facet["status"] = "read_needed" if supporting else "missing"
                unresolved.append(
                    {**requirement, "status": "read_needed" if supporting else "missing"}
                )
        facets = _records(covered_question.get("requirements"))
        covered_question["status"] = (
            "evidence_recorded"
            if facets and all(item["status"] == "evidence_recorded" for item in facets)
            else "missing"
        )
    assigned_questions = {
        str(requirement["question_id"])
        for identity, requirement in requirements.items()
        if identity in assigned_requirements
    }
    unresolved.sort(
        key=lambda requirement: (
            str(requirement["question_id"]) in evidenced_questions,
            str(requirement["question_id"]) in assigned_questions,
            str(requirement["id"]) in assigned_requirements,
        )
    )
    unassigned_requirements = [
        str(requirement["id"])
        for requirement in unresolved
        if str(requirement["id"]) not in assigned_requirements
    ]
    unassigned_questions = list(
        dict.fromkeys(
            str(requirement["question_id"])
            for requirement in unresolved
            if str(requirement["question_id"]) not in assigned_questions
        )
    )
    unanswered_questions = list(
        dict.fromkeys(
            str(requirement["question_id"])
            for requirement in unresolved
            if str(requirement["question_id"]) not in evidenced_questions
        )
    )
    limits = research_limits(run.budget_preset, run.research_policy_version)
    remaining_rounds = max(
        0, limits.collection_rounds - max((item.round_index for item in worker_tasks), default=0)
    )
    remaining_workers = max(0, limits.worker_tasks - len(worker_tasks))
    sources = list(
        dict.fromkeys(
            source
            for note in checkpoints
            for source in cast(list[str], note.get("examined_source_ids", []))
        )
    )
    if observed_reads:
        sources = list(dict.fromkeys([*sources, *(item.source_id for item in observed_reads)]))
    budget = dict(shared_budget or {})
    initial_seconds = run.execution_budget_seconds or limits.seconds
    collection_seconds = max(0.0, snapshot.remaining_seconds - initial_seconds * 0.25)
    action, reason = "research", "evidence_gaps"
    if not questions:
        action, reason = "plan", "brief_required"
    elif not unresolved and not open_conflicts and not open_gaps:
        action, reason = "stop", "evidence_unavailable" if unavailable else "coverage_recorded"
    decision = snapshot.checkpoint.get("collection_decision")
    valid_decision = isinstance(decision, dict) and snapshot.checkpoint.get(
        "collection_decision_basis"
    ) == collection_basis(snapshot.checkpoint, worker_notes)
    if valid_decision and isinstance(decision, dict) and decision.get("explanation"):
        if decision.get("reason") == "low_expected_value":
            action, reason = "stop", "low_expected_value"
        elif (
            decision.get("reason") == "evidence_unavailable"
            and unavailable
            and not unresolved
            and not open_conflicts
        ):
            action, reason = "stop", "evidence_unavailable"
        elif (
            decision.get("reason") == "sufficient"
            and not unresolved
            and not unavailable
            and not open_conflicts
            and not open_gaps
        ):
            action, reason = "stop", "coverage_recorded"
    if not remaining_rounds:
        action, reason = "stop", "collection_round_limit"
    elif not remaining_workers:
        action, reason = "stop", "worker_task_limit"
    if collection_seconds <= 0 or any(
        type(budget.get(key)) in (int, float) and cast(float, budget[key]) <= 0
        for key in (
            "collection_calls_remaining",
            "collection_tokens_remaining",
            "collection_cost_remaining",
            "collection_seconds_remaining",
        )
    ):
        action, reason = "stop", "finalization_reserve"
    if budget.get("budget_unknown") is True:
        action, reason = "stop", "usage_unknown"
    return {
        "coverage": coverage,
        "unresolved_requirements": unresolved,
        "unassigned_requirement_ids": unassigned_requirements,
        "unassigned_question_ids": unassigned_questions,
        "unanswered_question_ids": unanswered_questions,
        "unavailable_requirements": unavailable,
        "open_gaps": open_gaps,
        "open_conflicts": open_conflicts,
        "examined_source_ids": sources,
        "remaining_collection_rounds": remaining_rounds,
        "remaining_worker_tasks": remaining_workers,
        "collection_seconds_remaining": collection_seconds,
        "next_action": action,
        "reason": reason,
        "requires_report_review": True,
        "shared_budget": budget,
    }
