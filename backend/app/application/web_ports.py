"""Narrow persistence contract for evidence and shared web allowances."""

from typing import Protocol

from app.domain.web import (
    CitationBinding,
    CitationReference,
    ClearSourcesResult,
    CrawlPage,
    CrawlPageBatch,
    SourceDraft,
    SourceLines,
    SourcePage,
    WebCrawl,
    WebDispatch,
    WebReservation,
    WebRunState,
    WebScope,
    WebSource,
    WebUsage,
)


class WebRepository(Protocol):
    async def web_tools_ready(self, *, crawl: bool = False) -> bool: ...

    async def reserve_search_attempt(
        self, scope: WebScope, request_id: str, key_hash: str, monthly_limit: int
    ) -> None: ...

    async def open_web_run(self, dispatch: WebDispatch) -> WebRunState: ...

    async def reserve_web_budget(
        self, scope: WebScope, reservation_id: str, operation_key: str, usage: WebUsage
    ) -> WebReservation: ...

    async def settle_web_budget(
        self, scope: WebScope, reservation_id: str, usage: WebUsage
    ) -> WebReservation: ...

    async def record_web_source(
        self, scope: WebScope, reservation_id: str, source: SourceDraft
    ) -> WebSource: ...

    async def list_web_sources(
        self, session_id: str, offset: int = 0, limit: int = 20
    ) -> SourcePage: ...

    async def list_run_web_sources(
        self, session_id: str, run_id: str, limit: int = 40
    ) -> tuple[WebSource, ...]: ...

    async def read_web_source(
        self, session_id: str, source_id: str, start_line: int = 1, max_lines: int = 100
    ) -> SourceLines | None: ...

    async def clear_web_sources(self, session_id: str) -> ClearSourcesResult: ...

    async def create_web_crawl(
        self,
        scope: WebScope,
        crawl_id: str,
        seed_url: str,
        origin: str,
        path_prefix: str,
        max_pages: int,
        max_depth: int,
    ) -> WebCrawl: ...

    async def get_web_crawl(self, session_id: str, crawl_id: str) -> WebCrawl | None: ...

    async def enqueue_web_pages(
        self, scope: WebScope, crawl_id: str, pages: tuple[tuple[str, int], ...]
    ) -> int: ...

    async def claim_web_page(self, scope: WebScope, crawl_id: str) -> CrawlPage | None: ...

    async def complete_web_page(
        self, scope: WebScope, crawl_id: str, url: str, source_id: str
    ) -> CrawlPage: ...

    async def reserve_crawl_origin(
        self, scope: WebScope, crawl_id: str, url: str, page_attempt: int
    ) -> float: ...

    async def finalize_crawl_page(
        self,
        scope: WebScope,
        crawl_id: str,
        url: str,
        page_attempt: int,
        reservation_id: str,
        source: SourceDraft,
        usage: WebUsage,
        discovered_links: tuple[str, ...],
    ) -> CrawlPage: ...

    async def list_web_pages(
        self, session_id: str, crawl_id: str, offset: int = 0, limit: int = 200
    ) -> CrawlPageBatch: ...

    async def checkpoint_web_crawl(
        self, scope: WebScope, crawl_id: str, status: str, stop_reason: str | None = None
    ) -> WebCrawl: ...

    async def bind_web_citations(
        self,
        run_id: str,
        generation: int,
        model_call_id: str,
        citations: tuple[CitationReference, ...],
    ) -> tuple[CitationBinding, ...]: ...

    async def read_web_citations(
        self, session_id: str, model_call_id: str
    ) -> tuple[CitationBinding, ...]: ...
