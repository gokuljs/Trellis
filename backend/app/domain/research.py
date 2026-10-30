from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal, Self, cast
from uuid import UUID

from app.domain.web import CitationReference

MAX_RESEARCH_CHECKPOINT_BYTES = 256 * 1024
MAX_RESEARCH_REPORT_BYTES = 64 * 1024
MAX_RESEARCH_CITATIONS = 50
MAX_RESEARCH_CONCURRENCY = 2
REPORT_CALL_RESERVE = 4

ResearchRole = Literal["lead", "worker", "writer", "reviewer"]


def research_identifier(value: object) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("invalid research identifier")
    return value


def research_object(
    value: object, *, maximum: int = MAX_RESEARCH_CHECKPOINT_BYTES
) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("research state must be an object")
    _validate_json(value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("research state exceeds its limit")
    return cast(dict[str, object], json.loads(encoded))


def _validate_json(value: object, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("research state is too deeply nested")
    if isinstance(value, str) and "\0" in value:
        raise ValueError("research state contains a null character")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("research object keys must be strings")
            _validate_json(key, depth + 1)
            _validate_json(item, depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            _validate_json(item, depth + 1)


def _text(value: object, *, maximum: int = 16000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise ValueError("invalid research text")
    return value


def _integer(value: object, *, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid research integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("invalid research number")
    if value < 0:
        raise ValueError("invalid research number")
    return float(value)


class ResearchPhase(StrEnum):
    SCOPING = "scoping"
    RESEARCHING = "researching"
    CHECKING_CONFLICT = "checking_conflict"
    WAITING_FOR_INPUT = "waiting_for_input"
    WRITING = "writing"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    STOPPED = "stopped"


class ResearchTaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    model_calls: int
    tool_calls: int
    total_tokens: int
    seconds: int
    model_cost_usd: float
    search_calls: int
    page_attempts: int
    worker_tasks: int
    collection_rounds: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_RESEARCH_LIMITS = {
    "conservative": ResearchLimits(24, 64, 100_000, 600, 2.0, 8, 20, 4, 2),
    "longer": ResearchLimits(40, 128, 200_000, 1200, 5.0, 16, 40, 8, 4),
}


def research_limits(preset: str) -> ResearchLimits:
    try:
        return _RESEARCH_LIMITS[preset]
    except KeyError:
        raise ValueError("unknown research budget") from None


@dataclass(frozen=True, slots=True)
class ResearchTaskInput:
    id: str
    objective: str
    role: ResearchRole = "worker"
    round_index: int = 1

    def __post_init__(self) -> None:
        research_identifier(self.id)
        _text(self.objective)
        if self.role not in {"lead", "worker", "writer", "reviewer"}:
            raise ValueError("invalid research role")
        _integer(self.round_index, maximum=4)
        if self.role == "worker" and self.round_index < 1:
            raise ValueError("worker requires a collection round")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResearchTask:
    id: str
    run_id: str
    objective: str
    role: ResearchRole
    round_index: int
    status: ResearchTaskStatus
    revision: int
    checkpoint: dict[str, object]
    result: dict[str, object] | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        task = ResearchTaskInput(
            research_identifier(value.get("id")),
            _text(value.get("objective")),
            cast(ResearchRole, value.get("role")),
            _integer(value.get("round_index"), maximum=4),
        )
        return cls(
            task.id,
            research_identifier(value.get("run_id")),
            task.objective,
            task.role,
            task.round_index,
            ResearchTaskStatus(_text(value.get("status"))),
            _integer(value.get("revision")),
            research_object(value.get("checkpoint")),
            research_object(value.get("result")) if value.get("result") is not None else None,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResearchReservation:
    id: str
    run_id: str
    task_id: str
    model_call_id: str
    reserved_tokens: int
    reserved_cost: float
    status: Literal["reserved", "settled", "unknown"]
    finalization: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        status = value.get("status")
        if (
            status not in {"reserved", "settled", "unknown"}
            or type(value.get("finalization")) is not bool
        ):
            raise ValueError("invalid research reservation state")
        return cls(
            research_identifier(value.get("id")),
            research_identifier(value.get("run_id")),
            research_identifier(value.get("task_id")),
            research_identifier(value.get("model_call_id")),
            _integer(value.get("reserved_tokens")),
            _number(value.get("reserved_cost")),
            cast(Literal["reserved", "settled", "unknown"], status),
            cast(bool, value["finalization"]),
        )


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    run_id: str
    session_id: str
    user_id: str
    phase: ResearchPhase
    objective: str
    revision: int
    checkpoint: dict[str, object]
    tasks: tuple[ResearchTask, ...]
    pending_clarification: dict[str, object] | None
    remaining_seconds: float
    stop_reason: str | None = None
    report_model_call_id: str | None = None
    citations: tuple[CitationReference, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        raw_tasks = value.get("tasks")
        raw_citations = value.get("citations", [])
        if not isinstance(raw_tasks, list) or len(raw_tasks) > 16:
            raise ValueError("invalid research task collection")
        if not isinstance(raw_citations, list) or len(raw_citations) > MAX_RESEARCH_CITATIONS:
            raise ValueError("invalid research citation collection")
        tasks = tuple(ResearchTask.from_dict(research_object(item)) for item in raw_tasks)
        run_id = research_identifier(value.get("run_id"))
        if len({task.id for task in tasks}) != len(tasks) or any(
            task.run_id != run_id for task in tasks
        ):
            raise ValueError("research tasks belong to another run")
        citations = []
        for item in raw_citations:
            reference = research_object(item)
            citations.append(
                CitationReference(
                    research_identifier(reference.get("source_id")),
                    _integer(reference.get("start_line")),
                    _integer(reference.get("end_line")),
                )
            )
        stop_reason = value.get("stop_reason")
        return cls(
            run_id,
            research_identifier(value.get("session_id")),
            research_identifier(value.get("user_id")),
            ResearchPhase(_text(value.get("phase"))),
            _text(value.get("objective")),
            _integer(value.get("revision")),
            research_object(value.get("checkpoint")),
            tasks,
            research_object(value.get("pending_clarification"))
            if value.get("pending_clarification") is not None
            else None,
            _number(value.get("remaining_seconds")),
            _text(stop_reason, maximum=100) if stop_reason is not None else None,
            research_identifier(value.get("report_model_call_id"))
            if value.get("report_model_call_id") is not None
            else None,
            tuple(citations),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    id: str
    text: str
    citations: tuple[CitationReference, ...]

    def __post_init__(self) -> None:
        _text(self.id, maximum=100)
        _text(self.text, maximum=8000)
        if not self.citations or len(self.citations) > MAX_RESEARCH_CITATIONS:
            raise ValueError("research claim requires supporting citations")


@dataclass(frozen=True, slots=True)
class ResearchReport:
    markdown: str
    claims: tuple[ResearchClaim, ...]
    covered_question_ids: tuple[str, ...]
    stop_reason: str

    def __post_init__(self) -> None:
        _text(self.markdown, maximum=MAX_RESEARCH_REPORT_BYTES)
        if len(self.markdown.encode("utf-8")) > MAX_RESEARCH_REPORT_BYTES:
            raise ValueError("research report exceeds its limit")
        _text(self.stop_reason, maximum=100)
        if len({claim.id for claim in self.claims}) != len(self.claims):
            raise ValueError("research claim identifiers must be unique")
        if sum(len(claim.citations) for claim in self.claims) > MAX_RESEARCH_CITATIONS:
            raise ValueError("research report has too many citations")
        if any(claim.text not in self.markdown for claim in self.claims):
            raise ValueError("research claim is absent from the report")
        if len(set(self.covered_question_ids)) != len(self.covered_question_ids):
            raise ValueError("research question coverage is duplicated")
        for question in self.covered_question_ids:
            _text(question, maximum=100)

    @property
    def content_hash(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchReview:
    report_hash: str
    source_hashes: dict[str, str]
    accepted_claim_ids: tuple[str, ...]
    issues: tuple[str, ...]

    def __post_init__(self) -> None:
        for digest in (self.report_hash, *self.source_hashes.values()):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("invalid research review hash")
        if len(self.source_hashes) > MAX_RESEARCH_CITATIONS:
            raise ValueError("research review has too many sources")
        for source in self.source_hashes:
            research_identifier(source)
        if len(set(self.accepted_claim_ids)) != len(self.accepted_claim_ids):
            raise ValueError("research review duplicates a claim")
        for claim in self.accepted_claim_ids:
            _text(claim, maximum=100)
        for issue in self.issues:
            _text(issue, maximum=8000)
