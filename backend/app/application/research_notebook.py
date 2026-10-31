from collections.abc import Iterable
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.research import research_object

EvidenceDepth = Literal["source_excerpt", "abstract", "full_text", "listing"]


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
    status: Literal["open", "resolved", "unavailable"] = "open"
    citations: list[EvidenceReference] = Field(default_factory=list, max_length=10)


class ResearchBrief(NotebookArguments):
    scope: str = Field(min_length=1, max_length=8000)
    comparison_dimensions: list[str] = Field(default_factory=list, max_length=12)
    source_restrictions: list[str] = Field(default_factory=list, max_length=12)
    expected_output: str = Field(default="", max_length=2000)


class ResearchCheckpointV2(NotebookArguments):
    phase: Literal["scoping", "researching", "checking_conflict"] = "researching"
    scope: str = Field(default="", max_length=8000)
    brief: ResearchBrief | None = None
    subquestions: list[ResearchQuestion] = Field(default_factory=list, max_length=12)
    findings: list[SourcedFinding] = Field(default_factory=list, max_length=50)
    examined_source_ids: list[str] = Field(default_factory=list, max_length=40)
    gaps: list[ResearchIssue] = Field(default_factory=list, max_length=50)
    conflicts: list[ResearchIssue] = Field(default_factory=list, max_length=50)
    stop_reason: str | None = Field(default=None, max_length=100)


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
    for key in ("phase", "scope", "stop_reason", "brief"):
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
            if key == "findings" and any(
                requirements.get(identity) not in question_ids
                for identity in cast(list[str], item["requirement_ids"])
            ):
                raise ValueError("research finding refers to an unknown requirement")
            for reference in _records(item.get("citations")):
                UUID(str(reference["source_id"]))
                if cast(int, reference["end_line"]) < cast(int, reference["start_line"]):
                    raise ValueError("citation range is reversed")
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
