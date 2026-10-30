"""Serial crawl scheduling over leased, atomic evidence checkpoints."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.application.errors import ApplicationError
from app.application.retrieval_ports import CrawlRetriever
from app.application.secret_redaction import SecretRedactor
from app.application.tool_context import ToolExecutionContext
from app.application.web_ports import WebRepository
from app.application.web_source_safety import accepted_source, sensitive_url
from app.domain.crawl import CrawlScope
from app.domain.retrieval import MAX_RETRIEVAL_SECONDS, ExtractedDocumentResult, RetrievalRequest
from app.domain.web import (
    MAX_SOURCE_BYTES,
    CrawlPage,
    SourceDraft,
    SourceStatus,
    WebScope,
    WebUsage,
)

CRAWL_TRANSPORT_BYTES = 18 * 1024 * 1024
CRAWL_NETWORK_REQUESTS = 7


class WebCrawlArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    seed_url: str = Field(min_length=1, max_length=4096)
    path_prefix: str = Field(default="/", min_length=1, max_length=4096)
    max_pages: int = Field(default=5, ge=1, le=20)
    max_depth: int = Field(default=1, ge=0, le=2)


class WebCrawlService:
    def __init__(
        self, repository: WebRepository, retriever: CrawlRetriever | None, redactor: SecretRedactor
    ) -> None:
        self._repository = repository
        self._retriever = retriever
        self._redactor = redactor

    async def execute(
        self, arguments: WebCrawlArguments, scope: WebScope, context: ToolExecutionContext
    ) -> dict[str, object]:
        if self._retriever is None:
            return {"status": "unavailable", "stop_reason": "crawler_not_configured", "pages": []}
        if sensitive_url(arguments.seed_url, self._redactor):
            return {"status": "blocked", "stop_reason": "sensitive_url", "pages": []}
        try:
            seed, navigation = CrawlScope.from_seed(arguments.seed_url, arguments.path_prefix)
        except ValueError, UnicodeError:
            return {"status": "blocked", "stop_reason": "invalid_crawl_scope", "pages": []}
        if sensitive_url(seed, self._redactor):
            return {"status": "blocked", "stop_reason": "sensitive_url", "pages": []}
        crawl_id = str(uuid5(UUID(context.tool_call_id), "crawl"))
        crawl = await self._repository.create_web_crawl(
            scope,
            crawl_id,
            seed,
            navigation.origin,
            navigation.path_prefix,
            arguments.max_pages,
            arguments.max_depth,
        )
        if crawl.status != "running":
            return await self._summary(scope, crawl_id, crawl.status, crawl.stop_reason)
        await self._repository.enqueue_web_pages(scope, crawl_id, ((seed, 0),))
        while True:
            await context.assert_active()
            checkpoint = await self._repository.get_web_crawl(scope.session_id, crawl_id)
            if checkpoint is None:
                raise RuntimeError("Crawl checkpoint disappeared")
            if checkpoint.status != "running":
                return await self._summary(
                    scope, crawl_id, checkpoint.status, checkpoint.stop_reason
                )
            frontier = await self._repository.list_web_pages(scope.session_id, crawl_id)
            if all(item.status == "completed" for item in frontier.items):
                return await self._stop(scope, crawl_id, "frontier_exhausted", completed=True)
            if sum(item.status == "completed" for item in frontier.items) >= arguments.max_pages:
                return await self._stop(scope, crawl_id, "page_limit", completed=True)
            if context.remaining_seconds <= 1:
                return await self._stop(scope, crawl_id, "deadline")
            state = await self._repository.open_web_run(scope)
            remaining_requests = 300 - state.usage.network_requests
            remaining_bytes = 64 * 1024 * 1024 - state.usage.response_bytes
            if (
                state.usage.page_attempts >= 20
                or remaining_requests < 2
                or remaining_bytes <= 0
                or state.usage.extracted_bytes + MAX_SOURCE_BYTES > 8 * 1024 * 1024
            ):
                return await self._stop(scope, crawl_id, "budget_exhausted")
            page = await self._repository.claim_web_page(scope, crawl_id)
            if page is None:
                pages = await self._repository.list_web_pages(scope.session_id, crawl_id)
                reason = (
                    "page_limit"
                    if sum(item.attempt > 0 for item in pages.items) >= arguments.max_pages
                    and any(item.status != "completed" for item in pages.items)
                    else "frontier_exhausted"
                )
                return await self._stop(scope, crawl_id, reason, completed=True)
            reservation_id = str(
                uuid5(UUID(context.dispatch_id), f"crawl:{page.url}:attempt:{page.attempt}")
            )
            request = RetrievalRequest(
                page.url,
                max_requests=min(CRAWL_NETWORK_REQUESTS, remaining_requests),
                max_response_bytes=min(CRAWL_TRANSPORT_BYTES, remaining_bytes),
                timeout_seconds=min(MAX_RETRIEVAL_SECONDS, context.remaining_seconds),
                scope_origin=navigation.origin,
                scope_path_prefix=navigation.path_prefix,
            )
            try:
                await self._repository.reserve_web_budget(
                    scope,
                    reservation_id,
                    f"crawl:{crawl_id}:{page.attempt}:{reservation_id}",
                    WebUsage(
                        page_attempts=1,
                        network_requests=request.max_requests,
                        response_bytes=request.max_response_bytes,
                        extracted_bytes=MAX_SOURCE_BYTES,
                    ),
                )
            except ApplicationError as error:
                if error.code != "web_budget_exhausted":
                    raise
                return await self._stop(scope, crawl_id, "budget_exhausted")
            if not await self._pace(scope, crawl_id, page, context):
                return await self._stop(scope, crawl_id, "deadline")
            await context.assert_active()
            if context.remaining_seconds <= 1:
                return await self._stop(scope, crawl_id, "deadline")
            request = replace(
                request, timeout_seconds=min(request.timeout_seconds, context.remaining_seconds - 1)
            )
            try:
                async with asyncio.timeout(min(request.timeout_seconds, context.remaining_seconds)):
                    result = await self._retriever.crawl_document(request, context)
            except TimeoutError:
                await context.assert_active()
                result = ExtractedDocumentResult(
                    SourceStatus.TIMEOUT,
                    request.url,
                    error_code="crawl_page_timeout",
                    usage=WebUsage(
                        page_attempts=1,
                        network_requests=request.max_requests,
                        response_bytes=request.max_response_bytes,
                    ),
                )
            await context.assert_active()
            draft = self._draft(result, reservation_id, page.url)
            links = self._links(result, navigation) if page.depth < arguments.max_depth else ()
            usage = replace(result.usage, extracted_bytes=len(draft.text.encode()))
            try:
                await self._repository.finalize_crawl_page(
                    scope, crawl_id, page.url, page.attempt, reservation_id, draft, usage, links
                )
            except ApplicationError as error:
                if error.code != "web_storage_limit":
                    raise
                draft = replace(
                    draft, text="", status=SourceStatus.BUDGET_EXHAUSTED, error_code=error.code
                )
                await self._repository.finalize_crawl_page(
                    scope,
                    crawl_id,
                    page.url,
                    page.attempt,
                    reservation_id,
                    draft,
                    replace(usage, extracted_bytes=0),
                    (),
                )
                return await self._stop(scope, crawl_id, "storage_limit")

    async def _pace(
        self, scope: WebScope, crawl_id: str, page: CrawlPage, context: ToolExecutionContext
    ) -> bool:
        while True:
            await context.assert_active()
            wait = await self._repository.reserve_crawl_origin(
                scope, crawl_id, page.url, page.attempt
            )
            if wait <= 0:
                return True
            if wait >= context.remaining_seconds - 1:
                return False
            await asyncio.sleep(min(wait, 1.0))

    def _draft(self, result: ExtractedDocumentResult, reservation_id: str, url: str) -> SourceDraft:
        if result.requested_url != url or (result.status.citable and result.final_url != url):
            raise RuntimeError("Crawler returned an unexpected navigation")
        draft = accepted_source(
            SourceDraft(
                str(uuid5(UUID(reservation_id), "source")),
                url,
                result.final_url,
                result.canonical_url,
                result.title,
                result.publisher,
                result.published_at,
                datetime.now(UTC).isoformat(),
                result.extractor_version,
                result.text,
                result.status,
                result.error_code,
            ),
            self._redactor,
        )
        if any(
            value is not None and sensitive_url(value, self._redactor)
            for value in (result.final_url, result.canonical_url)
        ):
            draft = replace(
                draft,
                final_url=None
                if result.final_url is not None and sensitive_url(result.final_url, self._redactor)
                else draft.final_url,
                canonical_url=None
                if result.canonical_url is not None
                and sensitive_url(result.canonical_url, self._redactor)
                else draft.canonical_url,
                status=SourceStatus.PARTIAL if draft.status.citable else draft.status,
                error_code="sensitive_content_redacted"
                if draft.status.citable
                else draft.error_code,
            )
        return draft

    def _links(self, result: ExtractedDocumentResult, navigation: CrawlScope) -> tuple[str, ...]:
        accepted: set[str] = set()
        if result.status.citable:
            for link in result.links:
                if sensitive_url(link, self._redactor):
                    continue
                try:
                    normalized = navigation.accept(link)
                except ValueError, UnicodeError:
                    continue
                if not sensitive_url(normalized, self._redactor):
                    accepted.add(normalized)
        return tuple(sorted(accepted))

    async def _stop(
        self, scope: WebScope, crawl_id: str, reason: str, *, completed: bool = False
    ) -> dict[str, object]:
        status = "completed" if completed else "stopped"
        await self._repository.checkpoint_web_crawl(scope, crawl_id, status, reason)
        return await self._summary(scope, crawl_id, status, reason)

    async def _summary(
        self, scope: WebScope, crawl_id: str, status: str, reason: str | None
    ) -> dict[str, object]:
        pages = await self._repository.list_web_pages(scope.session_id, crawl_id)
        state = await self._repository.open_web_run(scope)
        outcomes: list[dict[str, object]] = []
        for page in pages.items:
            if page.status != "completed" or page.source_id is None:
                continue
            source = await self._repository.read_web_source(scope.session_id, page.source_id, 1, 1)
            if source is None:
                raise RuntimeError("Completed crawl page has no recorded source")
            outcomes.append(
                {
                    "source_id": source.source.id,
                    "status": source.source.status.value,
                    "error_code": source.source.error_code,
                }
            )
        return {
            "crawl_id": crawl_id,
            "status": status,
            "stop_reason": reason,
            "untrusted_sources": True,
            "pages": outcomes,
            "completed_pages": len(outcomes),
            "claimed_attempts": sum(page.attempt for page in pages.items),
            "run_page_attempts_charged": state.usage.page_attempts,
        }
