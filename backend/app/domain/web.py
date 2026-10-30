"""Recorded web evidence and bounded collection state."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

MAX_SOURCE_BYTES = 256 * 1024
MAX_SOURCE_LINES = MAX_SOURCE_BYTES + 1
MAX_RUN_TEXT_BYTES = 8 * 1024 * 1024
MAX_OWNER_TEXT_BYTES = 100 * 1024 * 1024
MAX_SOURCE_LINE_CHARS = 2048
MAX_SOURCE_READ_BYTES = 20 * 1024
MAX_SOURCE_READ_LINES = 200


def _text(value: object, name: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\0" in value:
        raise ValueError(f"invalid web {name}")
    return value


def _optional_text(value: object, name: str, limit: int = 4096) -> str | None:
    return None if value is None else _text(value, name, limit)


def _integer(value: object, name: str, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"invalid web {name}")
    return value


def _identifier(value: object, name: str) -> str:
    return str(UUID(_text(value, name, 36)))


def canonical_source_text(text: str) -> str:
    """Give stored lines a fixed identity and a bounded individual size."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\0", "")
    if not normalized:
        return ""
    lines = []
    for line in normalized.split("\n"):
        if line:
            lines.extend(
                line[offset : offset + MAX_SOURCE_LINE_CHARS]
                for offset in range(0, len(line), MAX_SOURCE_LINE_CHARS)
            )
        else:
            lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class WebUsage:
    search_calls: int = 0
    page_attempts: int = 0
    network_requests: int = 0
    response_bytes: int = 0
    extracted_bytes: int = 0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            _integer(value, name)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        names = (
            "search_calls",
            "page_attempts",
            "network_requests",
            "response_bytes",
            "extracted_bytes",
        )
        if set(value) != set(names):
            raise ValueError("invalid web usage fields")
        return cls(**{name: _integer(value[name], name) for name in names})


@dataclass(frozen=True, slots=True)
class WebDispatch:
    user_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    dispatch_id: str
    dispatch_attempt: int
    generation: int

    def __post_init__(self) -> None:
        for name in ("user_id", "session_id", "run_id", "tool_call_id", "dispatch_id"):
            _identifier(getattr(self, name), name)
        if _integer(self.dispatch_attempt, "dispatch attempt", 4) < 1:
            raise ValueError("invalid web dispatch attempt")
        _integer(self.generation, "generation")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WebScope(WebDispatch):
    evidence_epoch: int

    def __post_init__(self) -> None:
        super(WebScope, self).__post_init__()
        _integer(self.evidence_epoch, "evidence epoch")


@dataclass(frozen=True, slots=True)
class WebRunState:
    evidence_epoch: int
    usage: WebUsage


@dataclass(frozen=True, slots=True)
class WebReservation:
    id: str
    scope: WebScope
    operation_key: str
    reserved: WebUsage
    actual: WebUsage | None
    status: str


class SourceStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    OVERSIZED = "oversized"
    UNSUPPORTED = "unsupported"
    EXTRACTION_FAILED = "extraction_failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CLEARED = "cleared"

    @property
    def citable(self) -> bool:
        return self in {SourceStatus.SUCCESS, SourceStatus.PARTIAL}


@dataclass(frozen=True, slots=True)
class SourceDraft:
    id: str
    requested_url: str
    final_url: str | None
    canonical_url: str | None
    title: str | None
    publisher: str | None
    published_at: str | None
    fetched_at: str
    extractor_version: str
    text: str
    status: SourceStatus
    error_code: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, "source ID")
        _text(self.requested_url, "requested URL")
        _optional_text(self.final_url, "final URL")
        _optional_text(self.canonical_url, "canonical URL")
        _optional_text(self.title, "title", 1000)
        _optional_text(self.publisher, "publisher", 500)
        _optional_text(self.published_at, "publication date", 100)
        _text(self.fetched_at, "fetch time", 100)
        if datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("web fetch time must be timezone aware")
        _text(self.extractor_version, "extractor version", 100)
        _optional_text(self.error_code, "error code", 100)
        if self.text != canonical_source_text(self.text):
            raise ValueError("web source text must have canonical lines")
        if len(self.text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("web source text exceeds the source limit")
        if self.status is SourceStatus.CLEARED:
            raise ValueError("cleared sources cannot be created")
        if self.status.citable:
            if not self.text or self.final_url is None:
                raise ValueError("successful web evidence requires content and a final URL")
        elif self.text:
            raise ValueError("failed web sources cannot contain citable text")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WebSource:
    id: str
    user_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    evidence_epoch: int
    requested_url: str | None
    final_url: str | None
    canonical_url: str | None
    title: str | None
    publisher: str | None
    published_at: str | None
    fetched_at: str | None
    extractor_version: str | None
    content_hash: str | None
    line_count: int
    byte_count: int
    status: SourceStatus
    error_code: str | None
    created_at: str

    @classmethod
    def from_dict(cls, row: Mapping[str, object]) -> Self:
        identifying = {
            name: _optional_text(row.get(name), name, limit)
            for name, limit in (
                ("requested_url", 4096),
                ("final_url", 4096),
                ("canonical_url", 4096),
                ("title", 1000),
                ("publisher", 500),
                ("published_at", 100),
                ("fetched_at", 100),
                ("extractor_version", 100),
                ("content_hash", 64),
                ("error_code", 100),
            )
        }
        status = SourceStatus(_text(row.get("status"), "source status", 40))
        content_hash = identifying["content_hash"]
        if content_hash is not None and re.fullmatch(r"[0-9a-f]{64}", content_hash) is None:
            raise ValueError("invalid web content hash")
        byte_count = _integer(row.get("byte_count"), "source bytes", MAX_SOURCE_BYTES)
        line_count = _integer(row.get("line_count"), "source lines", MAX_SOURCE_LINES)
        if status.citable and (not content_hash or not line_count or not byte_count):
            raise ValueError("successful source has no recorded content")
        if status is SourceStatus.CLEARED and (
            any(value is not None for value in identifying.values()) or byte_count or line_count
        ):
            raise ValueError("cleared source contains identifying content")
        return cls(
            id=_identifier(row.get("id"), "source ID"),
            user_id=_identifier(row.get("user_id"), "source owner"),
            session_id=_identifier(row.get("session_id"), "source conversation"),
            run_id=_identifier(row.get("run_id"), "source run"),
            tool_call_id=_identifier(row.get("tool_call_id"), "source tool"),
            evidence_epoch=_integer(row.get("evidence_epoch"), "evidence epoch"),
            line_count=line_count,
            byte_count=byte_count,
            status=status,
            created_at=_text(row.get("created_at"), "source creation time", 100),
            **identifying,
        )


@dataclass(frozen=True, slots=True)
class SourcePage:
    items: tuple[WebSource, ...]
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class SourceLines:
    source: WebSource
    start_line: int
    lines: tuple[str, ...]
    total_lines: int


@dataclass(frozen=True, slots=True)
class ClearSourcesResult:
    evidence_epoch: int
    cleared_count: int


@dataclass(frozen=True, slots=True)
class WebCrawl:
    id: str
    user_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    evidence_epoch: int
    seed_url: str
    origin: str
    path_prefix: str
    max_pages: int
    max_depth: int
    status: str
    stop_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CrawlPage:
    crawl_id: str
    url: str
    depth: int
    status: str
    attempt: int
    generation: int | None
    source_id: str | None
    discovered_links: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CrawlPageBatch:
    items: tuple[CrawlPage, ...]
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class CitationReference:
    source_id: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        _identifier(self.source_id, "citation source")
        if not 1 <= self.start_line <= self.end_line <= MAX_SOURCE_LINES:
            raise ValueError("invalid citation lines")
        if self.end_line - self.start_line >= MAX_SOURCE_READ_LINES:
            raise ValueError("citation line range is too large")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CitationBinding:
    model_call_id: str
    source_id: str
    start_line: int
    end_line: int
    citation_index: int
