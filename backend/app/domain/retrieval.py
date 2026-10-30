"""Limits and outcomes of one isolated public-document retrieval."""

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.web import MAX_SOURCE_BYTES, SourceStatus, WebUsage, canonical_source_text

HTML_RAW_BYTES = 2 * 1024 * 1024
HTML_DECODED_BYTES = 4 * 1024 * 1024
PDF_RAW_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_NETWORK_REQUESTS = 300
MAX_RETRIEVAL_SECONDS = 30.0
MAX_REDIRECTS = 5
MAX_WORKER_REQUEST_BYTES = 64 * 1024
MAX_EXTRACTED_RESULT_BYTES = 1024 * 1024
MAX_DOCUMENT_LINKS = 32
EXTRACTOR_VERSION = "trellis-extract-v1"


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """Authorize total gateway bytes; raw/decoded document ceilings are independent."""

    url: str
    max_requests: int = MAX_REDIRECTS + 1
    max_response_bytes: int = PDF_RAW_BYTES
    timeout_seconds: float = MAX_RETRIEVAL_SECONDS
    scope_origin: str | None = None
    scope_path_prefix: str = "/"

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not 1 <= len(self.url) <= 4096:
            raise ValueError("invalid retrieval URL")
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= MAX_NETWORK_REQUESTS:
            raise ValueError("invalid retrieval request allowance")
        if (
            type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= MAX_RESPONSE_BYTES
        ):
            raise ValueError("invalid retrieval byte allowance")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 0 < self.timeout_seconds <= MAX_RETRIEVAL_SECONDS
        ):
            raise ValueError("invalid retrieval timeout")
        if (
            not isinstance(self.scope_path_prefix, str)
            or not self.scope_path_prefix.startswith("/")
            or len(self.scope_path_prefix) > 4096
        ):
            raise ValueError("invalid retrieval scope path")
        if self.scope_origin is not None and (
            not isinstance(self.scope_origin, str) or not 1 <= len(self.scope_origin) <= 4096
        ):
            raise ValueError("invalid retrieval scope origin")


class RetrievalStatus(StrEnum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    OVERSIZED = "oversized"
    UNSUPPORTED = "unsupported"
    ACCESS_DENIED = "access_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    status: RetrievalStatus
    requested_url: str
    final_url: str | None = None
    media_type: str | None = None
    body: bytes = field(default=b"", repr=False)
    usage: WebUsage = field(default_factory=WebUsage)
    error_code: str | None = None

    def __post_init__(self) -> None:
        if len(self.body) > PDF_RAW_BYTES:
            raise ValueError("retrieval result exceeds document limit")
        if self.status is RetrievalStatus.SUCCESS:
            if self.final_url is None or self.media_type not in {
                "text/html",
                "text/plain",
                "application/pdf",
            }:
                raise ValueError("successful retrieval requires URL and supported media type")
        elif self.body:
            raise ValueError("failed retrieval cannot contain document bytes")


@dataclass(frozen=True, slots=True)
class ExtractedDocumentResult:
    """Bounded worker output; document bytes never cross this application boundary."""

    status: SourceStatus
    requested_url: str
    final_url: str | None = None
    media_type: str | None = None
    text: str = field(default="", repr=False)
    usage: WebUsage = field(default_factory=WebUsage)
    error_code: str | None = None
    title: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    canonical_url: str | None = None
    links: tuple[str, ...] = ()
    extractor_version: str = EXTRACTOR_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.status, SourceStatus) or self.status is SourceStatus.CLEARED:
            raise ValueError("invalid document extraction status")
        if (
            not isinstance(self.text, str)
            or self.text != canonical_source_text(self.text)
            or len(self.text.encode("utf-8")) > MAX_SOURCE_BYTES
        ):
            raise ValueError("invalid extracted document text")
        for name, maximum, required in (
            ("requested_url", 4096, True),
            ("final_url", 4096, False),
            ("canonical_url", 4096, False),
            ("title", 1000, False),
            ("publisher", 500, False),
            ("published_at", 100, False),
            ("error_code", 100, False),
            ("extractor_version", 100, True),
        ):
            value = getattr(self, name)
            if value is None and not required:
                continue
            if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\0" in value:
                raise ValueError("invalid extracted document metadata")
        if (
            not isinstance(self.links, tuple)
            or len(self.links) > MAX_DOCUMENT_LINKS
            or any(
                not isinstance(link, str)
                or not 1 <= len(link.encode("utf-8")) <= 4096
                or "\0" in link
                for link in self.links
            )
        ):
            raise ValueError("invalid extracted document links")
        if self.status.citable:
            if (
                not self.text
                or self.final_url is None
                or self.media_type not in {"text/html", "text/plain", "application/pdf"}
            ):
                raise ValueError("successful extraction requires document text and metadata")
        elif self.text:
            raise ValueError("failed extraction cannot contain citable text")
        if self.media_type is not None and self.media_type not in {
            "text/html",
            "text/plain",
            "application/pdf",
        }:
            raise ValueError("invalid extracted document media type")
        if self.usage.extracted_bytes != len(self.text.encode("utf-8")):
            raise ValueError("extracted document usage differs from its text")
