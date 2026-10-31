"""Bind semantic coverage decisions to reviewed claims and observed source windows."""

from collections.abc import Iterable
from typing import cast

from app.domain.research import ResearchQuestionCoverage, ResearchReport, ResearchSnapshot


def _records(value: object) -> list[dict[str, object]]:
    return (
        [cast(dict[str, object], item) for item in value if isinstance(item, dict)]
        if isinstance(value, (list, tuple))
        else []
    )


def _identifiers(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 50
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError("invalid research question coverage")
    if len(set(value)) != len(value):
        raise ValueError("invalid research question coverage")
    return tuple(value)


def _observed(
    reference: dict[str, object], snapshot: ResearchSnapshot, hashes: dict[str, str]
) -> bool:
    start, end = reference.get("start_line"), reference.get("end_line")
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= start + 199:
        return False
    source = str(reference.get("source_id"))
    ranges = [
        item
        for read in snapshot.source_reads
        if read.get("source_id") == source and hashes.get(source) == read.get("content_hash")
        for item in _records(read.get("presented_ranges"))
    ]
    return all(
        any(
            type(item.get("start_line")) is int
            and type(item.get("end_line")) is int
            and cast(int, item["start_line"]) <= line <= cast(int, item["end_line"])
            for item in ranges
        )
        for line in range(start, end + 1)
    )


def _eligible_requirements(
    snapshot: ResearchSnapshot,
    hashes: dict[str, str],
    report: ResearchReport,
    accepted_claim_ids: tuple[str, ...],
) -> dict[str, set[str]]:
    questions = _records(snapshot.checkpoint.get("subquestions"))
    required = {
        str(item["id"]): item.get("depth", "source_excerpt")
        for question in questions
        for item in _records(question.get("evidence_requirements"))
    }
    notes: Iterable[dict[str, object]] = (
        snapshot.checkpoint,
        *(
            task.result or task.checkpoint
            for task in snapshot.tasks
            if task.role in {"lead", "worker"}
        ),
    )
    eligible: dict[str, set[str]] = {}
    for note in notes:
        for finding in _records(note.get("findings")):
            references = _records(finding.get("citations"))
            if not references or not all(_observed(ref, snapshot, hashes) for ref in references):
                continue
            claim_ids = {
                claim.id
                for claim in report.claims
                if claim.id in accepted_claim_ids
                and any(
                    citation.source_id == reference.get("source_id")
                    and citation.start_line <= cast(int, reference["end_line"])
                    and citation.end_line >= cast(int, reference["start_line"])
                    for citation in claim.citations
                    for reference in references
                )
            }
            for identity in cast(list[str], finding.get("requirement_ids", [])):
                depth = required.get(identity)
                if depth is not None and (
                    depth == "source_excerpt" or finding.get("evidence_depth") == depth
                ):
                    eligible.setdefault(identity, set()).update(claim_ids)
    return eligible


def complete_question_ids(
    coverage: Iterable[ResearchQuestionCoverage], snapshot: ResearchSnapshot
) -> set[str]:
    required = {
        str(question["id"]): {
            str(item["id"]) for item in _records(question.get("evidence_requirements"))
        }
        for question in _records(snapshot.checkpoint.get("subquestions"))
    }
    return {
        item.question_id
        for item in coverage
        if required.get(item.question_id)
        and required[item.question_id].issubset(item.requirement_ids)
    }


def review_question_coverage(
    value: object,
    report: ResearchReport,
    snapshot: ResearchSnapshot,
    accepted_claim_ids: tuple[str, ...],
    hashes: dict[str, str],
) -> tuple[tuple[ResearchQuestionCoverage, ...], tuple[str, ...]]:
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("invalid research question coverage")
    questions = {
        str(question["id"]): {
            str(item["id"]) for item in _records(question.get("evidence_requirements"))
        }
        for question in _records(snapshot.checkpoint.get("subquestions"))
    }
    eligible = _eligible_requirements(snapshot, hashes, report, accepted_claim_ids)
    coverage = []
    seen: set[str] = set()
    issues = []
    for raw in value:
        if not isinstance(raw, dict) or raw.keys() != {
            "question_id",
            "requirement_ids",
            "claim_ids",
        }:
            raise ValueError("invalid research question coverage")
        question = raw["question_id"]
        requirements, claims = _identifiers(raw["requirement_ids"]), _identifiers(raw["claim_ids"])
        if (
            not isinstance(question, str)
            or question in seen
            or question not in questions
            or not set(requirements).issubset(questions[question])
            or not set(claims).issubset(accepted_claim_ids)
        ):
            raise ValueError("invalid research question coverage")
        seen.add(question)
        supported = tuple(
            identity
            for identity in requirements
            if eligible.get(identity, set()).intersection(claims)
        )
        if set(supported) != set(requirements):
            issues.append(
                f"Question {question} lacks observed evidence at the required depth "
                "for some reported requirements."
            )
        if supported and claims:
            coverage.append(ResearchQuestionCoverage(question, supported, claims))
    complete = complete_question_ids(coverage, snapshot)
    for identity in report.covered_question_ids:
        if identity not in complete:
            issues.append(
                f"Question {identity} is marked covered without reviewed support for every "
                "evidence requirement. Reduce coverage or supply complete evidence."
            )
    return tuple(coverage), tuple(issues)
