"""Bounded report input recovered from authoritative saved page lines."""

import asyncio
import json
from collections.abc import Iterable
from itertools import zip_longest

from app.application.web_ports import WebRepository
from app.domain.research import ResearchSnapshot
from app.domain.web import SourceLines, SourceStatus, WebSource

MAX_REPORT_EVIDENCE_BYTES = 48 * 1024
MAX_REPORT_SOURCES = 40
_OMISSION_RESERVE = 8 * 1024
_READ_DEADLINE_SECONDS = 20
_WINDOW_LINES = 60


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
) -> tuple[dict[str, object], set[str]]:
    lines: dict[int, str] = {}
    first_count = source.line_count if source.line_count <= 200 else _WINDOW_LINES
    windows = [(1, first_count)]
    for start, count in windows:
        result = await web.read_web_source(snapshot.session_id, source.id, start, count)
        if result is None:
            return {
                **item,
                "title": None,
                "url": None,
                "fetched_at": None,
                "content_hash": None,
                "line_count": 0,
                "status": "unavailable",
                "excerpts": [],
            }, {"saved source is unavailable"}
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
    accepted: dict[int, str] = {}
    maximum = _size(item) + extra_allowance
    for number, text in _priorities(lines):
        candidate = {**accepted, number: text}
        if _size({**item, "excerpts": _ranges(candidate)}) <= maximum:
            accepted = candidate
    reasons = {"some saved lines omitted"} if len(accepted) < source.line_count else set()
    return {**item, "excerpts": _ranges(accepted)}, reasons


async def collect_report_evidence(
    web: WebRepository, snapshot: ResearchSnapshot
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + _READ_DEADLINE_SECONDS
    try:
        async with asyncio.timeout_at(deadline):
            sources = await web.list_run_web_sources(
                snapshot.session_id, snapshot.run_id, MAX_REPORT_SOURCES
            )
    except TimeoutError:
        return {
            "sources": [],
            "omissions": ["Saved source inventory exceeded the report read deadline."],
        }
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
    packet: dict[str, object] = {"sources": items, "omissions": []}
    citable = sum(source.status.citable for source in sources)
    extra = max(0, MAX_REPORT_EVIDENCE_BYTES - _size(packet) - _OMISSION_RESERVE) // max(1, citable)
    index = 0
    try:
        async with asyncio.timeout_at(deadline):
            for index, source in enumerate(sources):
                if not source.status.citable:
                    omissions[source.id].add("saved source is " + source.status.value)
                    continue
                items[index], reasons = await _hydrate(
                    web, snapshot, source, items[index], metadata_allowance, extra
                )
                omissions[source.id].update(reasons)
    except TimeoutError:
        for source in sources[index:]:
            if source.status.citable:
                omissions[source.id].add("saved excerpts exceeded the report read deadline")
    packet["omissions"] = [
        "Source " + identity + ": " + ", ".join(sorted(reasons)) + "."
        for identity, reasons in omissions.items()
        if reasons
    ]
    if _size(packet) > MAX_REPORT_EVIDENCE_BYTES:
        raise ValueError("research evidence packet exceeds its limit")
    return packet
