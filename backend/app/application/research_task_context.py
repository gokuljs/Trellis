"""Project task-owned research exchanges without changing their audit history."""

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    RunMessageRecord,
    ToolCallRecord,
    ToolCallStatus,
)
from app.domain.web import MAX_SOURCE_LINES, MAX_SOURCE_READ_LINES

_TERMINAL_TOOLS = frozenset(
    {
        ToolCallStatus.COMPLETED,
        ToolCallStatus.FAILED,
        ToolCallStatus.DENIED,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.TIMED_OUT,
    }
)


@dataclass(frozen=True, slots=True)
class ResearchReadRange:
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class ResearchReadObservation:
    tool_call_id: str
    message_ordinal: int
    source_id: str
    content_hash: str
    read_kind: str
    presented_ranges: tuple[ResearchReadRange, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_call_id": self.tool_call_id,
            "message_ordinal": self.message_ordinal,
            "source_id": self.source_id,
            "content_hash": self.content_hash,
            "read_kind": self.read_kind,
            "presented_ranges": [
                {"start_line": value.start_line, "end_line": value.end_line}
                for value in self.presented_ranges
            ],
        }


@dataclass(frozen=True, slots=True)
class ResearchTaskContext:
    messages: tuple[RunMessageRecord, ...]
    observed_reads: tuple[ResearchReadObservation, ...]
    unprocessed_reads: tuple[ResearchReadObservation, ...]
    checkpointed_read_through_ordinal: int
    read_through_ordinal: int
    has_open_tools: bool

    @property
    def checkpoint_required(self) -> bool:
        return len(self.unprocessed_reads) >= 2


def parse_research_read_observations(
    records: Sequence[Mapping[str, object]],
) -> tuple[ResearchReadObservation, ...]:
    if len(records) > 384:
        raise ValueError("research source read metadata exceeds its limit")
    observations = []
    for record in records:
        source_id, tool_id = record.get("source_id"), record.get("tool_call_id")
        content_hash, ordinal = record.get("content_hash"), record.get("message_ordinal")
        if (
            not isinstance(source_id, str)
            or not isinstance(tool_id, str)
            or not isinstance(content_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
            or type(ordinal) is not int
            or not 1 <= ordinal <= 2**63 - 1
            or record.get("read_kind") != "source_lines"
        ):
            raise ValueError("research source read metadata is invalid")
        try:
            if str(UUID(source_id)) != source_id or str(UUID(tool_id)) != tool_id:
                raise ValueError
        except ValueError:
            raise ValueError("research source read identity is invalid") from None
        raw_ranges = record.get("presented_ranges")
        if not isinstance(raw_ranges, list) or not 1 <= len(raw_ranges) <= MAX_SOURCE_READ_LINES:
            raise ValueError("research source read ranges are invalid")
        windows = []
        line_count = 0
        for raw in raw_ranges:
            if not isinstance(raw, dict):
                raise ValueError("research source read range is invalid")
            start, end = raw.get("start_line"), raw.get("end_line")
            if (
                type(start) is not int
                or type(end) is not int
                or not 1 <= start <= end <= MAX_SOURCE_LINES
            ):
                raise ValueError("research source read range is invalid")
            line_count += end - start + 1
            if line_count > MAX_SOURCE_READ_LINES:
                raise ValueError("research source read window exceeds its limit")
            windows.append(ResearchReadRange(start, end))
        observations.append(
            ResearchReadObservation(
                tool_id, ordinal, source_id, content_hash, "source_lines", tuple(windows)
            )
        )
    return tuple(observations)


def checkpoint_disposes_reads(
    arguments: Mapping[str, object], observations: Sequence[ResearchReadObservation]
) -> bool:
    references = []
    for key in ("findings", "gaps", "conflicts"):
        records = arguments.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                continue
            if not record["text"].strip() or not isinstance(record.get("citations"), list):
                continue
            for citation in record["citations"]:
                if not isinstance(citation, dict):
                    continue
                start, end = citation.get("start_line"), citation.get("end_line")
                if type(start) is int and type(end) is int and 1 <= start <= end:
                    references.append((citation.get("source_id"), start, end))
    return all(
        any(
            source_id == observation.source_id
            and any(
                start <= window.end_line and end >= window.start_line
                for window in observation.presented_ranges
            )
            for source_id, start, end in references
        )
        for observation in observations
    )


def bounded_research_notebook(
    state: Mapping[str, object], max_bytes: int = 32 * 1024
) -> dict[str, object]:
    value = deepcopy(dict(state))
    checkpoint = value.get("checkpoint")
    brief = value.get("research_brief")
    if not isinstance(checkpoint, dict):
        checkpoint = {}
        value["checkpoint"] = checkpoint
    if not isinstance(brief, dict):
        brief = {}
        value["research_brief"] = brief
    if isinstance(brief.get("subquestions"), list) and brief["subquestions"]:
        checkpoint.pop("subquestions", None)
    workers = value.get("worker_findings")
    notes = [checkpoint, brief]
    if isinstance(workers, list):
        notes.extend(worker for worker in workers if isinstance(worker, dict))
    candidates: list[tuple[dict[str, object], str, object]] = []
    omitted = {"findings": 0, "gaps": 0, "conflicts": 0, "summaries": 0}
    for notebook in notes:
        for key in ("findings", "gaps", "conflicts"):
            records = notebook.get(key)
            if not isinstance(records, list):
                continue
            notebook[key] = []
            candidates.extend((notebook, key, record) for record in records)
            omitted[key] += len(records)
        summary = notebook.pop("summary", None)
        if isinstance(summary, str) and summary:
            candidates.append((notebook, "summary", summary))
            omitted["summaries"] += 1
    value["omitted_counts"] = omitted

    def fits() -> bool:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= max_bytes

    if not fits():
        raise ValueError("mandatory research brief exceeds the active notebook limit")
    for notebook, key, record in candidates:
        if key == "summary":
            notebook[key] = record
        else:
            records = notebook[key]
            assert isinstance(records, list)
            records.append(record)
        if fits():
            omitted["summaries" if key == "summary" else key] -= 1
        elif key == "summary":
            notebook.pop(key)
        else:
            records = notebook[key]
            assert isinstance(records, list)
            records.pop()
    return value


def _object(content: str) -> dict[str, object]:
    try:
        value = json.loads(content)
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def _observed_read(
    message: RunMessageRecord, tool: ToolCallRecord
) -> ResearchReadObservation | None:
    value = _object(message.content)
    if (
        tool.name != "web_source_read"
        or tool.status is not ToolCallStatus.COMPLETED
        or value.get("status") not in {"success", "partial"}
        or value.get("substantive") is not True
        or value.get("read_kind") != "source_lines"
    ):
        return None
    try:
        observation = parse_research_read_observations(
            [{**value, "tool_call_id": tool.id, "message_ordinal": message.ordinal}]
        )[0]
    except ValueError:
        return None
    lines = value.get("untrusted_lines")
    if not isinstance(lines, list) or not lines:
        return None
    presented: set[int] = set()
    for line in lines:
        if (
            not isinstance(line, dict)
            or type(line.get("line")) is not int
            or not 1 <= line["line"] <= MAX_SOURCE_LINES
            or not isinstance(line.get("text"), str)
            or line["line"] in presented
        ):
            return None
        presented.add(line["line"])
    claimed = {
        number
        for window in observation.presented_ranges
        for number in range(window.start_line, window.end_line + 1)
    }
    if claimed != presented or len(presented) > MAX_SOURCE_READ_LINES:
        return None
    return observation


def _complete_exchange(
    assistant: RunMessageRecord,
    messages: Sequence[RunMessageRecord],
    tools: Sequence[ToolCallRecord],
    calls: Mapping[str, ModelCallRecord],
) -> bool:
    call = calls.get(assistant.model_call_id or "")
    if call is None or call.status is not ModelCallStatus.COMPLETED:
        return False
    if len(tools) != len(assistant.tool_calls):
        return False
    results = {message.tool_call_id for message in messages if message.role == "tool"}
    for requested in assistant.tool_calls:
        matches = [
            tool
            for tool in tools
            if tool.provider_call_id == requested.id and tool.name == requested.name
        ]
        if (
            len(matches) != 1
            or matches[0].status not in _TERMINAL_TOOLS
            or matches[0].id not in results
        ):
            return False
    return len(results) == len(tools)


def project_research_task_context(
    run_id: str,
    task_id: str,
    messages: Sequence[RunMessageRecord],
    tool_calls: Sequence[ToolCallRecord],
    model_calls: Sequence[ModelCallRecord],
) -> ResearchTaskContext:
    ordered = sorted(
        (item for item in messages if item.run_id == run_id and item.task_id == task_id),
        key=lambda item: (item.ordinal, item.id),
    )
    tools = {
        item.id: item for item in tool_calls if item.run_id == run_id and item.task_id == task_id
    }
    calls = {
        item.id: item for item in model_calls if item.run_id == run_id and item.task_id == task_id
    }
    assistants = {item.id: item for item in ordered if item.role == "assistant"}
    groups = {identity: [message] for identity, message in assistants.items()}
    orphans = []
    observed = []
    for message in ordered:
        if message.role != "tool":
            continue
        tool = tools.get(message.tool_call_id or "")
        if tool is None or tool.assistant_message_id not in assistants:
            orphans.append(message)
            continue
        groups[tool.assistant_message_id].append(message)
        observation = _observed_read(message, tool)
        if observation is not None:
            observed.append(observation)
    cursor = 0
    for message in ordered:
        tool = tools.get(message.tool_call_id or "")
        if (
            message.role != "tool"
            or tool is None
            or tool.name != "research_checkpoint"
            or tool.status is not ToolCallStatus.COMPLETED
            or _object(message.content).get("status") != "saved"
        ):
            continue
        assistant = assistants.get(tool.assistant_message_id)
        call = calls.get(assistant.model_call_id or "") if assistant is not None else None
        if call is None or call.status is not ModelCallStatus.COMPLETED:
            continue
        metadata = call.request_snapshot.get("research")
        if not isinstance(metadata, dict) or "read_through_ordinal" not in metadata:
            continue
        through = metadata["read_through_ordinal"]
        if (
            metadata.get("task_id") != task_id
            or type(through) is not int
            or through < 0
            or assistant is None
            or through >= assistant.ordinal
            or through not in {0, *(read.message_ordinal for read in observed)}
        ):
            raise ValueError("research checkpoint processing cursor is inconsistent")
        cursor = max(cursor, through)
    complete = {
        identity
        for identity, assistant in assistants.items()
        if _complete_exchange(
            assistant,
            groups[identity],
            [tool for tool in tools.values() if tool.assistant_message_id == identity],
            calls,
        )
    }
    recent = set(sorted(complete, key=lambda identity: assistants[identity].ordinal)[-2:])
    retained = [*orphans]
    for identity, group in groups.items():
        if (
            identity in complete
            and identity not in recent
            and max(item.ordinal for item in group) <= cursor
        ):
            continue
        retained.extend(group)
    return ResearchTaskContext(
        tuple(sorted(retained, key=lambda item: (item.ordinal, item.id))),
        tuple(observed),
        tuple(read for read in observed if read.message_ordinal > cursor),
        cursor,
        max((read.message_ordinal for read in observed), default=0),
        bool(orphans) or len(complete) != len(groups),
    )
