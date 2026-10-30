"""Bounded public-search discovery data, separate from fetched evidence."""

from dataclasses import dataclass
from enum import StrEnum

MAX_SEARCH_QUERY_CHARS = 512
MAX_SEARCH_RESULTS = 10
MAX_SEARCH_RESPONSE_BYTES = 1024 * 1024


class SearchStatus(StrEnum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    INVALID_KEY = "invalid_key"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    OVERSIZED = "oversized"


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str

    def __post_init__(self) -> None:
        for value, limit in ((self.title, 1000), (self.url, 4096), (self.snippet, 2048)):
            if not isinstance(value, str) or len(value) > limit or "\0" in value:
                raise ValueError("invalid search result field")
        if not self.url:
            raise ValueError("search result requires a URL")


@dataclass(frozen=True, slots=True)
class SearchResult:
    status: SearchStatus
    hits: tuple[SearchHit, ...] = ()
    error_code: str | None = None
    response_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, SearchStatus) or len(self.hits) > MAX_SEARCH_RESULTS:
            raise ValueError("invalid search result")
        if self.status is not SearchStatus.SUCCESS and self.hits:
            raise ValueError("failed search cannot return discovery hits")
        if (
            type(self.response_bytes) is not int
            or not 0 <= self.response_bytes <= MAX_SEARCH_RESPONSE_BYTES
        ):
            raise ValueError("invalid search response bytes")
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or not 1 <= len(self.error_code) <= 100
        ):
            raise ValueError("invalid search error code")
