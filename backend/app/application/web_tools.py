"""Ordinary run tools for public search and immutable page evidence."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from app.application.errors import ApplicationError
from app.application.retrieval_ports import BrowserRenderer, CrawlRetriever, DocumentRetriever
from app.application.search_ports import SearchProvider
from app.application.secret_redaction import COMMON_SECRET_REDACTOR, SecretRedactor
from app.application.tool_context import ToolExecutionContext
from app.application.tools import MAX_RESULT_BYTES, ToolRegistration
from app.application.web_crawl import WebCrawlArguments, WebCrawlService
from app.application.web_evidence import WEB_ERROR_MESSAGES, WebEvidenceService
from app.application.web_source_safety import accepted_source
from app.application.web_source_summary import bound_source_summaries, source_summary
from app.domain.capabilities import RuntimeCapability
from app.domain.recovery import ToolRecoveryPolicy
from app.domain.research import RESEARCH_POLICY_V2
from app.domain.retrieval import (
    MAX_RENDER_SECONDS,
    MAX_RETRIEVAL_SECONDS,
    ExtractedDocumentResult,
    RenderRequest,
    RetrievalRequest,
)
from app.domain.search import SearchResult, SearchStatus
from app.domain.web import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_LINES,
    SourceDraft,
    SourceLines,
    SourceStatus,
    WebScope,
    WebSource,
    WebUsage,
)

READ_TRANSPORT_BYTES = 18 * 1024 * 1024
_JSON_LIMIT = MAX_RESULT_BYTES - 1
ProgressPublisher = Callable[[ToolExecutionContext, dict[str, object]], Awaitable[None]]


@dataclass(slots=True)
class _SourceReadLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WebSearchArguments(_Arguments):
    query: str = Field(min_length=1, max_length=512)
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def public_query(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Enter one bounded public query")
        return value


class WebReadArguments(_Arguments):
    # Previously dispatched renders must remain resumable when Chromium is unavailable.
    render: SkipJsonSchema[bool] = False
    urls: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1, max_length=5
    )

    @field_validator("urls")
    @classmethod
    def url_characters(cls, value: list[str]) -> list[str]:
        if any(ord(character) < 32 or ord(character) == 127 for url in value for character in url):
            raise ValueError("Invalid URL characters")
        return value


class WebRenderArguments(WebReadArguments):
    render: bool = False


class _SourceIdentityArguments(_Arguments):
    source_id: str = Field(min_length=36, max_length=36)

    @field_validator("source_id")
    @classmethod
    def source_identifier(cls, value: str) -> str:
        return str(UUID(value))


class WebSourceArguments(_SourceIdentityArguments):
    start_line: int = Field(default=1, ge=1, le=MAX_SOURCE_LINES)
    max_lines: int = Field(default=100, ge=1, le=200)


class WebSourceScanArguments(_SourceIdentityArguments):
    start_line: int = Field(default=1, ge=1, le=MAX_SOURCE_LINES)
    max_scan_lines: int = Field(default=2000, ge=1, le=4000)


class WebSourceFindArguments(WebSourceScanArguments):
    query: str = Field(min_length=1, max_length=256)
    case_sensitive: bool = False
    max_matches: int = Field(default=8, ge=1, le=20)
    context_lines: int = Field(default=2, ge=0, le=5)

    @field_validator("query")
    @classmethod
    def literal_query(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("Enter one bounded literal search string")
        return value


class WebSourceOutlineArguments(WebSourceScanArguments):
    max_headings: int = Field(default=30, ge=1, le=50)


_SECTION_HEADING = re.compile(
    r"^(?:#{1,6}\s+\S|\d+(?:\.\d+)*[.)]?\s+[A-Z]|"
    r"(?:abstract|introduction|background|methods?|methodology|approach|experiments?|"
    r"evaluation|results|discussion|limitations|conclusions?|references|pricing|features|"
    r"careers|open positions|requirements|responsibilities|compensation)\s*$)",
    re.IGNORECASE,
)


def _operation_id(context: ToolExecutionContext, name: str) -> str:
    return str(uuid5(UUID(context.tool_call_id), name))


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class WebToolService:
    def __init__(
        self,
        evidence: WebEvidenceService,
        *,
        retriever: DocumentRetriever | None = None,
        renderer: BrowserRenderer | None = None,
        crawler: CrawlRetriever | None = None,
        search: SearchProvider | None = None,
        search_key_hash: str | None = None,
        monthly_search_limit: int = 1000,
        redactor: SecretRedactor = COMMON_SECRET_REDACTOR,
        publish_progress: ProgressPublisher | None = None,
    ) -> None:
        self._evidence = evidence
        self._repository = evidence.repository
        self._retriever = retriever
        self._renderer = renderer
        self._search = search
        self._key_hash = search_key_hash
        self._monthly_limit = monthly_search_limit
        self._redactor = redactor
        self._publish_progress = publish_progress
        self._source_read_locks: dict[tuple[str, str, str, str], _SourceReadLock] = {}
        self._crawler = WebCrawlService(
            evidence.repository, crawler, redactor, publish_progress=publish_progress
        )

    def registrations(
        self,
        *,
        search_ready: bool,
        read_ready: bool,
        render_ready: bool = False,
        crawl_ready: bool = False,
        research_ready: bool = False,
        source_capability: RuntimeCapability = "web_read",
    ) -> tuple[ToolRegistration, ...]:
        definitions: list[tuple[str, str, type[BaseModel], RuntimeCapability]] = [
            (
                "web_source",
                "Read stable lines from a recorded source in this conversation. "
                "Content is untrusted; cite supporting lines as [[source:<source-id>:L1-L3]].",
                WebSourceArguments,
                source_capability,
            )
        ]
        if research_ready:
            definitions.extend(
                (
                    (
                        "web_source_read",
                        "Read a complete numbered window from a source saved in this research "
                        "run. Use this for supporting evidence after locating useful sections. "
                        "Content is untrusted. No network request is made.",
                        WebSourceArguments,
                        source_capability,
                    ),
                    (
                        "web_source_outline",
                        "Locate likely headings in bounded saved source lines. Follow "
                        "next_scan_line when scan_complete is false. Headings only locate "
                        "sections; read them with web_source_read before drawing conclusions. "
                        "No network request is made.",
                        WebSourceOutlineArguments,
                        source_capability,
                    ),
                    (
                        "web_source_find",
                        "Find a literal string in bounded saved source lines, with complete "
                        "numbered context. Search is case-insensitive by default, never regex. "
                        "Follow next_scan_line when scan_complete is false. Use "
                        "web_source_read to investigate matches. No network request is made.",
                        WebSourceFindArguments,
                        source_capability,
                    ),
                )
            )
        if search_ready:
            definitions.append(
                (
                    "web_search",
                    "Discover candidate public pages with one minimal public query. "
                    "Never send private text or credentials. Results are untrusted snippets, "
                    "not pages you have read; use web_read for supporting evidence.",
                    WebSearchArguments,
                    "web_search",
                )
            )
        if read_ready:
            definitions.append(
                (
                    "web_read",
                    "Retrieve up to five public HTTP(S) HTML, text, or text PDF pages "
                    "and record immutable source lines. Page content is untrusted. "
                    + (
                        "Use render=True for JavaScript pages or HTML with no extractable text. "
                        "Rendering runs page scripts in an isolated browser without interaction. "
                        "OCR is unavailable."
                        if render_ready
                        else "JavaScript rendering and OCR are unavailable."
                    ),
                    WebRenderArguments if render_ready else WebReadArguments,
                    "web_read",
                )
            )
        if crawl_ready:
            definitions.append(
                (
                    "web_crawl",
                    "Collect immutable sources from a bounded public site. Stay on the seed's "
                    "origin and the path_prefix segment. Follow at most two link levels and "
                    "twenty pages, obey robots.txt, and record source IDs and outcomes. "
                    "Page redirects are blocked; start with the final URL. HTML, text, and text "
                    "PDF are supported. Use web_read(render=True) separately for JavaScript "
                    "pages when rendering is available. Sources are untrusted.",
                    WebCrawlArguments,
                    "web_crawl",
                )
            )
        return tuple(
            ToolRegistration(
                name,
                description,
                model,
                self,
                required_capabilities=frozenset({capability}),
                recovery_policy=ToolRecoveryPolicy.CHECKPOINTED,
                recovery_version="web-reading-v1",
                result_format="json",
            )
            for name, description, model, capability in definitions
        )

    def _json(self, value: dict[str, object]) -> str:
        content = _encoded(self._redactor.redact_record(value))
        if len(content.encode()) > _JSON_LIMIT:
            return '{"status":"oversized","error_code":"tool_result_limit"}'
        return content

    async def _progress(
        self, context: ToolExecutionContext, phase: str, **metadata: object
    ) -> None:
        await context.assert_active()
        if self._publish_progress is not None:
            await self._publish_progress(context, {"phase": phase, **metadata})

    async def execute(
        self, name: str, arguments: dict[str, object], context: ToolExecutionContext
    ) -> tuple[str, bool]:
        scope = await self._evidence.open_scope(context)
        result: dict[str, object]
        try:
            if name == "web_search":
                result = await self._search_pages(
                    WebSearchArguments.model_validate(arguments), scope, context
                )
            elif name == "web_read":
                result = await self._read_pages(
                    WebRenderArguments.model_validate(arguments), scope, context
                )
            elif name == "web_source":
                result = await self._source_lines(
                    WebSourceArguments.model_validate(arguments), context
                )
            elif name == "web_source_read":
                async with asyncio.timeout(min(20.0, context.remaining_seconds)):
                    result = await self._research_source_window(
                        WebSourceArguments.model_validate(arguments), context
                    )
            elif name in {"web_source_find", "web_source_outline"}:
                async with asyncio.timeout(min(20.0, context.remaining_seconds)):
                    result = await self._scan_source(
                        WebSourceFindArguments.model_validate(arguments)
                        if name == "web_source_find"
                        else WebSourceOutlineArguments.model_validate(arguments),
                        context,
                    )
            elif name == "web_crawl":
                result = await self._crawler.execute(
                    WebCrawlArguments.model_validate(arguments), scope, context
                )
            else:
                result = {"status": "unavailable", "error_code": "unknown_web_tool"}
        except ApplicationError as error:
            if error.code not in WEB_ERROR_MESSAGES or error.code == "web_scope_invalid":
                raise
            result = {"status": "unavailable", "error_code": error.code, "message": error.message}
        await context.assert_active()
        return self._json(result), False

    async def _search_pages(
        self, arguments: WebSearchArguments, scope: WebScope, context: ToolExecutionContext
    ) -> dict[str, object]:
        if self._search is None or self._key_hash is None:
            return {"status": "unavailable", "error_code": "search_not_configured"}
        if context.dispatch_attempt > 1:
            return {"status": "interrupted", "error_code": "search_not_replayed", "not_read": True}
        reservation_id = _operation_id(context, "search")
        maximum = WebUsage(search_calls=1, network_requests=1, response_bytes=1024 * 1024)
        await self._repository.reserve_web_budget(scope, reservation_id, "search", maximum)
        await context.assert_active()
        await self._repository.reserve_search_attempt(
            scope, reservation_id, self._key_hash, self._monthly_limit
        )
        await self._progress(context, "web_search", status="started")
        query = self._redactor.redact_text(arguments.query)
        try:
            async with asyncio.timeout(min(30.0, context.remaining_seconds)):
                result = await self._search.search(query, arguments.max_results, context)
        except TimeoutError:
            result = SearchResult(
                SearchStatus.TIMEOUT, error_code="search_timeout", response_bytes=1024 * 1024
            )
        await context.assert_active()
        await self._repository.settle_web_budget(
            scope,
            reservation_id,
            WebUsage(search_calls=1, network_requests=1, response_bytes=result.response_bytes),
        )
        await self._progress(
            context, "web_search", status=result.status.value, results=len(result.hits)
        )
        value = self._search_result(result)
        value["query_redacted"] = query != arguments.query
        return value

    def _search_result(self, result: SearchResult) -> dict[str, object]:
        hits: list[dict[str, object]] = []
        value: dict[str, object] = {
            "status": result.status.value,
            "error_code": result.error_code,
            "untrusted_discovery": True,
            "not_read": True,
            "results": hits,
            "omitted_results": len(result.hits),
        }
        for hit in result.hits:
            candidate: dict[str, object] = {
                "title": hit.title,
                "url": hit.url,
                "snippet": hit.snippet,
            }
            hits.append(candidate)
            value["omitted_results"] = len(result.hits) - len(hits)
            if len(_encoded(self._redactor.redact_record(value)).encode()) > _JSON_LIMIT - 128:
                hits.pop()
                value["omitted_results"] = len(result.hits) - len(hits)
                break
        return value

    async def _read_pages(
        self, arguments: WebRenderArguments, scope: WebScope, context: ToolExecutionContext
    ) -> dict[str, object]:
        pages: list[dict[str, object]] = []
        for index, url in enumerate(arguments.urls):
            reuse = (
                context.run_mode == "research"
                and context.research_policy_version == RESEARCH_POLICY_V2
            )
            if reuse and context.dispatch_attempt == 1:
                async with self._source_read_lock(url, context):
                    page = await self._read_single_page(
                        url, index, arguments, scope, context, reuse=True
                    )
            else:
                page = await self._read_single_page(
                    url, index, arguments, scope, context, reuse=False
                )
            page["input_index"] = index
            pages.append(page)
        value: dict[str, object] = {
            "status": "completed",
            "untrusted_sources": True,
            "pages": pages,
        }
        return bound_source_summaries(value, pages, self._redactor)

    @asynccontextmanager
    async def _source_read_lock(
        self, url: str, context: ToolExecutionContext
    ) -> AsyncIterator[None]:
        key = context.user_id, context.session_id, context.run_id, url
        entry = self._source_read_locks.setdefault(key, _SourceReadLock())
        entry.users += 1
        try:
            async with asyncio.timeout(context.remaining_seconds), entry.lock:
                await context.assert_active()
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                self._source_read_locks.pop(key, None)

    async def _saved_run_source(
        self, url: str, scope: WebScope, context: ToolExecutionContext, *, render: bool
    ) -> dict[str, object] | None:
        await context.assert_active()
        async with asyncio.timeout(min(10.0, context.remaining_seconds)):
            sources = await self._repository.list_run_web_sources(
                context.session_id, context.run_id, 40
            )
            await context.assert_active()
            for source in sources:
                if (
                    source.user_id != context.user_id
                    or source.session_id != context.session_id
                    or source.run_id != context.run_id
                ):
                    raise ApplicationError(
                        "web_scope_invalid", WEB_ERROR_MESSAGES["web_scope_invalid"]
                    )
                if not source.status.citable:
                    continue
                if source.evidence_epoch != scope.evidence_epoch:
                    raise ApplicationError(
                        "web_scope_invalid", WEB_ERROR_MESSAGES["web_scope_invalid"]
                    )
                if url not in {source.requested_url, source.final_url}:
                    continue
                if render and not (source.extractor_version or "").startswith("trellis-render-"):
                    continue
                if not self._reusable_source(source):
                    raise ApplicationError(
                        "web_scope_invalid", WEB_ERROR_MESSAGES["web_scope_invalid"]
                    )
                result = await self._repository.read_web_source(context.session_id, source.id, 1, 3)
                await context.assert_active()
                if result is None:
                    raise ApplicationError(
                        "source_not_found", WEB_ERROR_MESSAGES["source_not_found"]
                    )
                if result.source != source or result.start_line != 1 or not result.lines:
                    raise ApplicationError(
                        "web_scope_invalid", WEB_ERROR_MESSAGES["web_scope_invalid"]
                    )
                page = await source_summary(self._repository, result, self._redactor)
                await context.assert_active()
                page.update({"reused": True, "reused_run_source": True})
                return page
        return None

    @staticmethod
    def _reusable_source(source: WebSource) -> bool:
        return (
            source.content_hash is not None
            and re.fullmatch(r"[0-9a-f]{64}", source.content_hash) is not None
            and source.line_count > 0
            and source.byte_count > 0
            and source.fetched_at is not None
        )

    async def _read_single_page(
        self,
        url: str,
        index: int,
        arguments: WebRenderArguments,
        scope: WebScope,
        context: ToolExecutionContext,
        *,
        reuse: bool,
    ) -> dict[str, object]:
        source_id = _operation_id(context, f"source:{index}")
        existing = await self._repository.read_web_source(context.session_id, source_id, 1, 3)
        await context.assert_active()
        if existing is not None:
            if (
                existing.source.run_id != context.run_id
                or existing.source.tool_call_id != context.tool_call_id
                or existing.source.requested_url != self._accepted_metadata(url, 4096)
            ):
                raise ApplicationError("web_scope_invalid", WEB_ERROR_MESSAGES["web_scope_invalid"])
            page = await source_summary(self._repository, existing, self._redactor)
            page["reused"] = True
            return page
        if context.dispatch_attempt > 1:
            return {
                "status": "interrupted",
                "error_code": "read_not_replayed",
                "not_attempted": True,
            }
        if reuse:
            page = await self._saved_run_source(url, scope, context, render=arguments.render)
            if page is not None:
                return page
        if arguments.render and self._renderer is None:
            return {
                "status": "unavailable",
                "error_code": "renderer_not_configured",
                "not_attempted": True,
            }
        if not arguments.render and self._retriever is None:
            return {
                "status": "unavailable",
                "error_code": "reader_not_configured",
                "not_attempted": True,
            }
        return await self._read_page(url, index, source_id, scope, context, render=arguments.render)

    async def _read_page(
        self,
        url: str,
        index: int,
        source_id: str,
        scope: WebScope,
        context: ToolExecutionContext,
        *,
        render: bool,
    ) -> dict[str, object]:
        reservation_id = _operation_id(context, f"page:{index}")
        request = (
            RenderRequest(
                url,
                max_response_bytes=READ_TRANSPORT_BYTES,
                timeout_seconds=min(MAX_RENDER_SECONDS, context.remaining_seconds),
            )
            if render
            else RetrievalRequest(
                url,
                max_response_bytes=READ_TRANSPORT_BYTES,
                timeout_seconds=min(MAX_RETRIEVAL_SECONDS, context.remaining_seconds),
            )
        )
        maximum = WebUsage(
            page_attempts=1,
            network_requests=request.max_requests,
            response_bytes=request.max_response_bytes,
            extracted_bytes=MAX_SOURCE_BYTES,
        )
        try:
            await self._repository.reserve_web_budget(
                scope, reservation_id, f"page:{index}", maximum
            )
        except ApplicationError as error:
            if error.code != "web_budget_exhausted":
                raise
            return {"status": "budget_exhausted", "error_code": error.code, "not_attempted": True}
        await self._progress(context, "web_read", input_index=index, status="started")
        if self._redactor.redact_text(url) != url:
            result = ExtractedDocumentResult(
                status=SourceStatus.BLOCKED,
                requested_url=url,
                error_code="sensitive_url",
                usage=WebUsage(page_attempts=1),
            )
        elif isinstance(request, RenderRequest):
            assert self._renderer is not None
            result = await self._renderer.render_document(request, context)
        else:
            assert self._retriever is not None
            result = await self._retriever.read_document(request, context)
        await context.assert_active()
        draft = SourceDraft(
            source_id,
            result.requested_url,
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
        )
        draft = accepted_source(
            draft,
            self._redactor,
            discovered_links=result.links if result.media_type == "text/html" else None,
        )
        try:
            await self._repository.record_web_source(scope, reservation_id, draft)
        except ApplicationError as error:
            if error.code != "web_storage_limit":
                raise
            await context.assert_active()
            draft = replace(
                draft, text="", status=SourceStatus.BUDGET_EXHAUSTED, error_code=error.code
            )
            await self._repository.record_web_source(scope, reservation_id, draft)
        await context.assert_active()
        usage = replace(result.usage, extracted_bytes=len(draft.text.encode()))
        await self._repository.settle_web_budget(scope, reservation_id, usage)
        lines = await self._repository.read_web_source(context.session_id, source_id, 1, 3)
        await self._progress(
            context, "web_read", input_index=index, status=draft.status.value, source_id=source_id
        )
        if lines is None:
            raise ApplicationError("source_not_found", WEB_ERROR_MESSAGES["source_not_found"])
        return await source_summary(self._repository, lines, self._redactor)

    def _accepted_metadata(self, value: str, limit: int) -> str:
        return self._redactor.redact_source_text(value)[:limit]

    async def _source_lines(
        self, arguments: WebSourceArguments, context: ToolExecutionContext
    ) -> dict[str, object]:
        result = await self._evidence.read_source(
            context.session_id, arguments.source_id, arguments.start_line, arguments.max_lines
        )
        value: dict[str, object] = {
            "status": result.source.status.value,
            "error_code": result.source.error_code,
            "source_id": result.source.id,
            "content_hash": result.source.content_hash,
            "total_lines": result.total_lines,
            "start_line": result.start_line,
            "untrusted_lines": [],
            "display_redacted": result.source.error_code == "sensitive_content_redacted",
            "next_line": result.start_line,
        }
        lines: list[dict[str, object]] = []
        value["untrusted_lines"] = lines
        for offset, line in enumerate(result.lines):
            safe = self._redactor.redact_text(line)
            lines.append({"line": result.start_line + offset, "text": safe})
            value["next_line"] = result.start_line + len(lines)
            value["display_redacted"] = bool(value["display_redacted"]) or safe != line
            if len(_encoded(self._redactor.redact_record(value)).encode()) > _JSON_LIMIT - 64:
                lines.pop()
                value["next_line"] = result.start_line + len(lines)
                break
        value["has_more"] = result.start_line + len(lines) <= result.total_lines
        return value

    async def _research_lines(
        self, source_id: str, start: int, count: int, context: ToolExecutionContext
    ) -> SourceLines:
        await context.assert_active()
        result = await self._evidence.read_source(context.session_id, source_id, start, count)
        await context.assert_active()
        source = result.source
        if (
            source.id != source_id
            or source.user_id != context.user_id
            or source.session_id != context.session_id
            or source.run_id != context.run_id
            or result.start_line != start
            or len(result.lines) > count
        ):
            raise ApplicationError("source_not_found", WEB_ERROR_MESSAGES["source_not_found"])
        return result

    async def _research_source_window(
        self, arguments: WebSourceArguments, context: ToolExecutionContext
    ) -> dict[str, object]:
        result = await self._research_lines(
            arguments.source_id, arguments.start_line, arguments.max_lines, context
        )
        value = self._scan_metadata(result, "source_lines")
        lines: list[dict[str, object]] = []
        value["untrusted_lines"] = lines
        value["next_line"] = result.start_line
        value["has_more"] = result.start_line <= result.total_lines
        for offset, line in enumerate(result.lines):
            lines.append({"line": result.start_line + offset, "text": line})
            value["next_line"] = result.start_line + len(lines)
            value["has_more"] = result.start_line + len(lines) <= result.total_lines
            value["presented_ranges"] = [
                {"start_line": result.start_line, "end_line": result.start_line + len(lines) - 1}
            ]
            value["substantive"] = True
            if not self._research_result_fits(value):
                lines.pop()
                break
        value["next_line"] = result.start_line + len(lines)
        value["has_more"] = result.start_line + len(lines) <= result.total_lines
        value["presented_ranges"] = (
            [{"start_line": result.start_line, "end_line": result.start_line + len(lines) - 1}]
            if lines
            else []
        )
        value["substantive"] = bool(lines)
        return value

    def _scan_metadata(self, result: SourceLines, kind: str) -> dict[str, object]:
        return {
            "status": result.source.status.value,
            "error_code": result.source.error_code,
            "source_id": result.source.id,
            "content_hash": result.source.content_hash,
            "total_lines": result.total_lines,
            "start_line": result.start_line,
            "read_kind": kind,
            "substantive": False,
            "presented_ranges": [],
            "untrusted_content": True,
        }

    def _research_result_fits(self, value: dict[str, object]) -> bool:
        return len(_encoded(self._redactor.redact_record(value)).encode()) <= _JSON_LIMIT - 256

    async def _scan_source(
        self,
        arguments: WebSourceFindArguments | WebSourceOutlineArguments,
        context: ToolExecutionContext,
    ) -> dict[str, object]:
        first = await self._research_lines(
            arguments.source_id, arguments.start_line, min(200, arguments.max_scan_lines), context
        )
        stop = min(first.total_lines + 1, arguments.start_line + arguments.max_scan_lines)
        saved_lines = list(first.lines[: stop - arguments.start_line])
        cursor = arguments.start_line + len(saved_lines)
        while cursor < stop:
            batch = await self._research_lines(
                arguments.source_id, cursor, min(200, stop - cursor), context
            )
            if (
                batch.source.content_hash != first.source.content_hash
                or batch.total_lines != first.total_lines
                or batch.source.status != first.source.status
            ):
                raise ApplicationError("source_not_found", WEB_ERROR_MESSAGES["source_not_found"])
            if not batch.lines:
                break
            saved_lines.extend(batch.lines)
            cursor += len(batch.lines)
        value = self._scan_metadata(
            first, "find" if isinstance(arguments, WebSourceFindArguments) else "outline"
        )
        values: list[dict[str, object]] = []
        ranges: list[dict[str, int]] = []
        value["matches" if isinstance(arguments, WebSourceFindArguments) else "headings"] = values
        value["presented_ranges"] = ranges
        value["scan_complete"] = cursor > first.total_lines
        value["next_scan_line"] = cursor
        limit = (
            arguments.max_matches
            if isinstance(arguments, WebSourceFindArguments)
            else arguments.max_headings
        )
        for offset, line in enumerate(saved_lines):
            number = arguments.start_line + offset
            entry: dict[str, object]
            if isinstance(arguments, WebSourceFindArguments):
                query = arguments.query if arguments.case_sensitive else arguments.query.casefold()
                candidate = line if arguments.case_sensitive else line.casefold()
                if query not in candidate:
                    continue
                left = max(0, offset - arguments.context_lines)
                right = min(len(saved_lines), offset + arguments.context_lines + 1)
                entry = {
                    "match_line": number,
                    "start_line": arguments.start_line + left,
                    "untrusted_lines": [
                        {"line": arguments.start_line + index, "text": saved_lines[index]}
                        for index in range(left, right)
                    ],
                }
                presented = {
                    "start_line": arguments.start_line + left,
                    "end_line": arguments.start_line + right - 1,
                }
            else:
                stripped = line.strip()
                underlined = offset + 1 < len(saved_lines) and bool(
                    re.fullmatch(r"(?:={3,}|-{3,})", saved_lines[offset + 1].strip())
                )
                if (
                    not stripped
                    or len(stripped) > 200
                    or not (_SECTION_HEADING.match(stripped) or underlined)
                ):
                    continue
                entry = {"line": number, "text": line}
                presented = {"start_line": number, "end_line": number}
            values.append(entry)
            ranges.append(presented)
            if (
                isinstance(arguments, WebSourceFindArguments)
                and len(values) == 1
                and not self._research_result_fits(value)
            ):
                entry["start_line"] = number
                entry["untrusted_lines"] = [{"line": number, "text": line}]
                entry["context_omitted"] = True
                presented["start_line"] = number
                presented["end_line"] = number
            if not self._research_result_fits(value):
                values.pop()
                ranges.pop()
                value["next_scan_line"] = number
                value["scan_complete"] = False
                break
            if len(values) >= limit:
                value["next_scan_line"] = number + 1
                value["scan_complete"] = number >= first.total_lines
                break
        return value
