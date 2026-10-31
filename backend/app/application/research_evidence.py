"""Bounded report input recovered from authoritative saved page lines."""

import asyncio
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from itertools import zip_longest

from app.application.web_ports import WebRepository
from app.domain.research import MAX_RESEARCH_CITATIONS, ResearchSnapshot
from app.domain.web import (
    MAX_SOURCE_READ_BYTES,
    CitationReference,
    SourceLines,
    SourceStatus,
    WebSource,
)

MAX_REPORT_EVIDENCE_BYTES = 48 * 1024
MAX_REPORT_SOURCES = 40
_OMISSION_RESERVE = 8 * 1024
_READ_DEADLINE_SECONDS = 20
_WINDOW_LINES = 60


@dataclass(frozen=True, slots=True)
class _FindingSupport:
    task_id: str | None
    finding_id: str
    question_ids: tuple[str, ...]
    references: tuple[CitationReference, ...]

    def record(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "finding_id": self.finding_id,
            "question_ids": list(self.question_ids),
            "citations": [asdict(reference) for reference in self.references],
        }


def _findings(snapshot: ResearchSnapshot) -> list[_FindingSupport]:
    checkpoints = [(None, snapshot.checkpoint)] + [
        (task.id, {**task.checkpoint, **(task.result or {})})
        for task in snapshot.tasks
        if task.role in {"lead", "worker"}
    ]
    questions = []
    findings = []
    for task_id, checkpoint in checkpoints:
        values = checkpoint.get("subquestions", [])
        if isinstance(values, list):
            questions.extend(
                value["id"]
                for value in values
                if isinstance(value, dict) and isinstance(value.get("id"), str)
            )
        records = []
        for kind in ("findings", "conflicts", "gaps"):
            entries = checkpoint.get(kind, [])
            if isinstance(entries, list):
                records.extend((kind, entry) for entry in entries[:50])
        for kind, value in records:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                continue
            if kind != "findings" and (
                value.get("status") == "resolved" or not value.get("citations")
            ):
                continue
            question_ids = value.get("question_ids", [])
            if not isinstance(question_ids, list):
                question_ids = []
            references = []
            citations = value.get("citations", [])
            if isinstance(citations, list) and len(citations) <= 10:
                try:
                    for citation in citations:
                        if not isinstance(citation, dict):
                            raise ValueError("invalid finding citation")
                        references.append(
                            CitationReference(
                                citation.get("source_id"),
                                citation.get("start_line"),
                                citation.get("end_line"),
                            )
                        )
                except ValueError, TypeError, AttributeError:
                    references = []
            findings.append(
                _FindingSupport(
                    task_id,
                    value["id"] if kind == "findings" else kind + ":" + value["id"],
                    tuple(identity for identity in question_ids[:12] if isinstance(identity, str)),
                    tuple(dict.fromkeys(references)),
                )
            )
    ordered = []
    remaining = list(findings)
    question_ids = tuple(dict.fromkeys(questions))
    while remaining:
        selected = []
        for question_id in question_ids:
            match = next(
                (finding for finding in remaining if question_id in finding.question_ids), None
            )
            if match is not None:
                selected.append(match)
                remaining.remove(match)
        if not selected:
            ordered.extend(remaining)
            break
        ordered.extend(selected)
    return ordered


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _scope(source: WebSource, snapshot: ResearchSnapshot) -> None:
    if (source.user_id, source.session_id, source.run_id) != (
        snapshot.user_id,
        snapshot.session_id,
        snapshot.run_id,
    ):
        raise ValueError("research evidence belongs to another account or run")


def _metadata(source: WebSource, allowance: int) -> tuple[dict[str, object], set[str]]:
    title = (
        source.title.encode("utf-8")[:160].decode("utf-8", errors="ignore")
        if source.title
        else None
    )
    item: dict[str, object] = {
        "source_id": source.id,
        "title": title,
        "url": source.final_url or source.requested_url,
        "status": source.status.value,
        "fetched_at": source.fetched_at,
        "line_count": source.line_count,
        "content_hash": source.content_hash,
        "excerpts": [],
    }
    reasons = set()
    if title != source.title:
        reasons.add("source metadata shortened")
    if _size(item) > allowance:
        item["url"] = None
        reasons.add("source metadata shortened")
    return item, reasons


def _ranges(lines: dict[int, str]) -> list[dict[str, object]]:
    ranges: list[dict[str, object]] = []
    previous = -1
    for number, text in sorted(lines.items()):
        if number != previous + 1:
            ranges.append({"start_line": number, "lines": []})
        group = ranges[-1]["lines"]
        assert isinstance(group, list)
        group.append(text)
        previous = number
    return ranges


def _unavailable(item: dict[str, object]) -> dict[str, object]:
    return {
        **item,
        "title": None,
        "url": None,
        "fetched_at": None,
        "content_hash": None,
        "line_count": 0,
        "status": "unavailable",
        "excerpts": [],
    }


def _priorities(lines: dict[int, str]) -> Iterable[tuple[int, str]]:
    ordered = sorted(lines.items())
    width = (len(ordered) + 2) // 3
    front, middle, tail = ordered[:width], ordered[width : 2 * width], ordered[2 * width :]
    center = len(middle) // 2
    middle = [item for _, item in sorted(enumerate(middle), key=lambda item: abs(item[0] - center))]
    for group in zip_longest(front, middle, reversed(tail)):
        yield from (item for item in group if item is not None)


def _checked_lines(
    result: SourceLines, original: WebSource, snapshot: ResearchSnapshot, start: int, count: int
) -> None:
    _scope(result.source, snapshot)
    if result.source.id != original.id or result.start_line != start:
        raise ValueError("research evidence identity changed")
    if result.source.status is SourceStatus.CLEARED:
        return
    if (
        result.source.content_hash != original.content_hash
        or result.total_lines != original.line_count
        or result.source.line_count != original.line_count
        or len(result.lines) > count
        or start + len(result.lines) - 1 > original.line_count
        or any(not isinstance(line, str) for line in result.lines)
    ):
        raise ValueError("research evidence content changed")


async def _hydrate(
    web: WebRepository,
    snapshot: ResearchSnapshot,
    source: WebSource,
    item: dict[str, object],
    metadata_allowance: int,
    extra_allowance: int,
    pinned: dict[int, str],
) -> tuple[dict[str, object], set[str]]:
    if len(pinned) == source.line_count:
        return item, set()
    lines: dict[int, str] = {}
    first_count = source.line_count if source.line_count <= 200 else _WINDOW_LINES
    windows = [(1, first_count)]
    for start, count in windows:
        result = await web.read_web_source(snapshot.session_id, source.id, start, count)
        if result is None:
            return _unavailable(item), {"saved source is unavailable"}
        _checked_lines(result, source, snapshot, start, count)
        if result.source.status is SourceStatus.CLEARED:
            cleared, _ = _metadata(result.source, metadata_allowance)
            return cleared, {"saved source was cleared"}
        lines.update((start + offset, text) for offset, text in enumerate(result.lines))
        if len(windows) == 1 and len(result.lines) < source.line_count:
            sample = min(_WINDOW_LINES, max(1, len(result.lines)))
            for sampled_start in (
                max(1, source.line_count // 2 - sample // 2),
                max(1, source.line_count - sample + 1),
            ):
                window = sampled_start, min(sample, source.line_count - sampled_start + 1)
                if window not in windows:
                    windows.append(window)
    accepted = dict(pinned)
    maximum = _size(item) + extra_allowance
    for number, text in _priorities(lines):
        candidate = {**accepted, number: text}
        if _size({**item, "excerpts": _ranges(candidate)}) <= maximum:
            accepted = candidate
    reasons = {"some saved lines omitted"} if len(accepted) < source.line_count else set()
    return {**item, "excerpts": _ranges(accepted)}, reasons


async def _supporting_lines(
    web: WebRepository,
    snapshot: ResearchSnapshot,
    source: WebSource,
    reference: CitationReference,
    cache: dict[int, str],
) -> tuple[dict[int, str], WebSource | None]:
    if reference.end_line > source.line_count or not source.status.citable:
        return {}, source
    if all(number in cache for number in range(reference.start_line, reference.end_line + 1)):
        lines = {
            number: cache[number] for number in range(reference.start_line, reference.end_line + 1)
        }
        return (
            lines
            if len("\n".join(lines.values()).encode("utf-8")) <= MAX_SOURCE_READ_BYTES
            else {},
            source,
        )
    count = reference.end_line - reference.start_line + 1
    result = await web.read_web_source(
        snapshot.session_id, reference.source_id, reference.start_line, count
    )
    if result is None:
        return {}, None
    _checked_lines(result, source, snapshot, reference.start_line, count)
    if not result.source.status.citable:
        return {}, result.source
    cache.update((reference.start_line + offset, text) for offset, text in enumerate(result.lines))
    if len(result.lines) != count:
        return {}, result.source
    return (
        dict(zip(range(reference.start_line, reference.end_line + 1), result.lines, strict=True)),
        source,
    )


def _finish(
    packet: dict[str, object],
    omissions: dict[str, set[str]],
    omitted: list[dict[str, object]],
) -> dict[str, object]:
    packet["omissions"] = [
        "Source " + identity + ": " + ", ".join(sorted(reasons)) + "."
        for identity, reasons in omissions.items()
        if reasons
    ]
    packet["omitted_finding_count"] = len(omitted)
    details: list[dict[str, object]] = []
    packet["omitted_findings"] = details
    packet["omitted_findings_truncated"] = False
    for finding in omitted:
        if _size({**packet, "omitted_findings": [*details, finding]}) <= MAX_REPORT_EVIDENCE_BYTES:
            details.append(finding)
    packet["omitted_findings_truncated"] = len(details) < len(omitted)
    if _size(packet) > MAX_REPORT_EVIDENCE_BYTES:
        raise ValueError("research evidence packet exceeds its limit")
    return packet


async def collect_report_evidence(
    web: WebRepository, snapshot: ResearchSnapshot
) -> dict[str, object]:
    findings = _findings(snapshot)
    deadline = asyncio.get_running_loop().time() + _READ_DEADLINE_SECONDS
    try:
        async with asyncio.timeout_at(deadline):
            sources = await web.list_run_web_sources(
                snapshot.session_id, snapshot.run_id, MAX_REPORT_SOURCES
            )
    except TimeoutError:
        return _finish(
            {"sources": [], "admitted_findings": []},
            {"inventory": {"saved source inventory exceeded the report read deadline"}},
            [
                {**finding.record(), "reason": "saved source inventory is unavailable"}
                for finding in findings
            ],
        )
    if len(sources) > MAX_REPORT_SOURCES or len({source.id for source in sources}) != len(sources):
        raise ValueError("research source inventory exceeds its limit")
    for source in sources:
        _scope(source, snapshot)
    metadata_allowance = (MAX_REPORT_EVIDENCE_BYTES // 2) // max(1, len(sources))
    items = []
    omissions: dict[str, set[str]] = {}
    for source in sources:
        item, reasons = _metadata(source, metadata_allowance)
        items.append(item)
        omissions[source.id] = reasons
    admitted: list[dict[str, object]] = []
    omitted: list[dict[str, object]] = []
    packet: dict[str, object] = {
        "sources": items,
        "omissions": [],
        "admitted_findings": admitted,
        "omitted_findings": [],
        "omitted_finding_count": 0,
        "omitted_findings_truncated": False,
    }
    by_id = {source.id: (index, source) for index, source in enumerate(sources)}
    cache: dict[str, dict[int, str]] = {}
    pinned: dict[str, dict[int, str]] = {}
    used_references: set[CitationReference] = set()
    unavailable: set[str] = set()
    _finding_index = 0
    try:
        async with asyncio.timeout_at(deadline):
            for _finding_index, finding in enumerate(findings):
                record = finding.record()
                if not finding.references:
                    omitted.append({**record, "reason": "finding has no valid saved support"})
                    continue
                if len(used_references.union(finding.references)) > MAX_RESEARCH_CITATIONS:
                    omitted.append({**record, "reason": "report citation binding limit"})
                    continue
                candidate_items = [dict(item) for item in items]
                candidate_lines: dict[str, dict[int, str]] = {}
                failure = None
                for reference in finding.references:
                    if reference.source_id not in by_id or reference.source_id in unavailable:
                        failure = "saved supporting source is unavailable"
                        break
                    index, source = by_id[reference.source_id]
                    lines, current = await _supporting_lines(
                        web, snapshot, source, reference, cache.setdefault(source.id, {})
                    )
                    if not lines:
                        if current is None:
                            items[index] = _unavailable(items[index])
                            unavailable.add(source.id)
                        elif current.status is SourceStatus.CLEARED:
                            items[index], _ = _metadata(current, metadata_allowance)
                            unavailable.add(source.id)
                        omissions[source.id].add("complete supporting range is unavailable")
                        failure = "complete supporting range is unavailable"
                        break
                    candidate_lines.setdefault(source.id, dict(pinned.get(source.id, {}))).update(
                        lines
                    )
                    candidate_items[index]["excerpts"] = _ranges(candidate_lines[source.id])
                if failure is not None:
                    omitted.append({**record, "reason": failure})
                    continue
                candidate = {
                    **packet,
                    "sources": candidate_items,
                    "admitted_findings": [*admitted, record],
                }
                if _size(candidate) > MAX_REPORT_EVIDENCE_BYTES - _OMISSION_RESERVE:
                    omitted.append(
                        {**record, "reason": "complete support exceeds report input limit"}
                    )
                    continue
                items[:] = candidate_items
                pinned.update(candidate_lines)
                admitted.append(record)
                used_references.update(finding.references)
    except TimeoutError:
        omitted.extend(
            {**finding.record(), "reason": "supporting evidence exceeded the report read deadline"}
            for finding in findings[_finding_index:]
        )
    citable = sum(source.status.citable for source in sources)
    extra = max(0, MAX_REPORT_EVIDENCE_BYTES - _size(packet) - _OMISSION_RESERVE) // max(1, citable)
    index = 0
    try:
        async with asyncio.timeout_at(deadline):
            for index, source in enumerate(sources):
                if not source.status.citable:
                    omissions[source.id].add("saved source is " + source.status.value)
                    continue
                if source.id in unavailable:
                    continue
                items[index], reasons = await _hydrate(
                    web,
                    snapshot,
                    source,
                    items[index],
                    metadata_allowance,
                    extra,
                    pinned.get(source.id, {}),
                )
                omissions[source.id].update(reasons)
                if items[index]["status"] in {"unavailable", SourceStatus.CLEARED.value}:
                    unavailable.add(source.id)
    except TimeoutError:
        for source in sources[index:]:
            if source.status.citable:
                omissions[source.id].add("saved excerpts exceeded the report read deadline")
    for record in admitted[:]:
        references = record["citations"]
        assert isinstance(references, list)
        if any(reference["source_id"] in unavailable for reference in references):
            admitted.remove(record)
            omitted.append({**record, "reason": "saved supporting source is unavailable"})
    return _finish(packet, omissions, omitted)
