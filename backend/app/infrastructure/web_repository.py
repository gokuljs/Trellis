"""Verified-user RPC adapter for immutable evidence and collection checkpoints."""

from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import UUID

from app.application.errors import ApplicationError
from app.domain.web import (
    MAX_SOURCE_READ_BYTES,
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


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RuntimeError("Supabase returned invalid web evidence")
    return cast(dict[str, object], value)


def _rows(value: object, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > limit:
        raise RuntimeError("Supabase returned an invalid web evidence list")
    return [_object(item) for item in value]


def _string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"Supabase returned invalid web {key}")
    return value


def _integer(row: Mapping[str, object], key: str, maximum: int = 2**63 - 1) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RuntimeError(f"Supabase returned invalid web {key}")
    return value


def _optional_string(row: Mapping[str, object], key: str) -> str | None:
    return None if row.get(key) is None else _string(row, key)


class WebRepositoryMixin:
    _user_id: UUID

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> object:
        raise NotImplementedError

    def _run_capability(self, run_id: str) -> str:
        raise NotImplementedError

    async def _web_call(
        self, action: str, payload: dict[str, object], *, run_id: str | None = None
    ) -> object:
        capability = self._run_capability(run_id) if run_id is not None else None
        retryable = action in {
            "open_run",
            "reserve",
            "settle",
            "record_source",
            "create_crawl",
            "enqueue_pages",
            "complete_page",
            "checkpoint_crawl",
            "bind_citations",
        }
        for attempt in range(2 if retryable else 1):
            try:
                return await self._request(
                    "POST",
                    "rpc/web_mutate",
                    payload={"p_action": action, "p_payload": payload, "p_capability": capability},
                )
            except ApplicationError as error:
                if not retryable or attempt or error.code != "database_unavailable":
                    raise
        raise AssertionError("bounded web mutation retry exhausted")

    def _scope_payload(self, scope: WebDispatch) -> dict[str, object]:
        if scope.user_id != str(self._user_id):
            raise ValueError("web scope belongs to another account")
        return scope.to_dict()

    def _source(self, value: object, session_id: str) -> WebSource:
        try:
            source = WebSource.from_dict(_object(value))
        except ValueError:
            raise RuntimeError("Supabase returned invalid source metadata") from None
        if source.user_id != str(self._user_id) or source.session_id != session_id:
            raise RuntimeError("Supabase returned another conversation's source")
        return source

    def _reservation(self, value: object, scope: WebScope) -> WebReservation:
        row = _object(value)
        fields = scope.to_dict()
        if any(row.get(name) != expected for name, expected in fields.items()):
            raise RuntimeError("Supabase returned another dispatch's reservation")
        try:
            actual = row.get("actual")
            result = WebReservation(
                id=str(UUID(_string(row, "id"))),
                scope=scope,
                operation_key=_string(row, "operation_key"),
                reserved=WebUsage.from_dict(_object(row.get("reserved"))),
                actual=WebUsage.from_dict(_object(actual)) if actual is not None else None,
                status=_string(row, "status"),
            )
        except ValueError:
            raise RuntimeError("Supabase returned an invalid web reservation") from None
        if result.status not in {"reserved", "settled"} or (
            (result.actual is not None) != (result.status == "settled")
        ):
            raise RuntimeError("Supabase returned an inconsistent web reservation")
        if result.actual is not None and any(
            result.actual.to_dict()[name] > maximum
            for name, maximum in result.reserved.to_dict().items()
        ):
            raise RuntimeError("Supabase returned an oversized web settlement")
        return result

    async def open_web_run(self, dispatch: WebDispatch) -> WebRunState:
        row = _object(
            await self._web_call("open_run", self._scope_payload(dispatch), run_id=dispatch.run_id)
        )
        return WebRunState(
            _integer(row, "evidence_epoch"), WebUsage.from_dict(_object(row.get("usage")))
        )

    async def reserve_web_budget(
        self, scope: WebScope, reservation_id: str, operation_key: str, usage: WebUsage
    ) -> WebReservation:
        result = self._reservation(
            await self._web_call(
                "reserve",
                {
                    **self._scope_payload(scope),
                    "reservation_id": reservation_id,
                    "operation_key": operation_key,
                    "usage": usage.to_dict(),
                },
                run_id=scope.run_id,
            ),
            scope,
        )
        if (
            result.id != reservation_id
            or result.operation_key != operation_key
            or result.reserved != usage
        ):
            raise RuntimeError("Supabase changed the reserved web allowance")
        return result

    async def settle_web_budget(
        self, scope: WebScope, reservation_id: str, usage: WebUsage
    ) -> WebReservation:
        result = self._reservation(
            await self._web_call(
                "settle",
                {
                    **self._scope_payload(scope),
                    "reservation_id": reservation_id,
                    "usage": usage.to_dict(),
                },
                run_id=scope.run_id,
            ),
            scope,
        )
        if result.id != reservation_id or result.actual != usage:
            raise RuntimeError("Supabase changed the completed web usage")
        return result

    async def record_web_source(
        self, scope: WebScope, reservation_id: str, source: SourceDraft
    ) -> WebSource:
        result = self._source(
            await self._web_call(
                "record_source",
                {
                    **self._scope_payload(scope),
                    "reservation_id": reservation_id,
                    "source": source.to_dict(),
                },
                run_id=scope.run_id,
            ),
            scope.session_id,
        )
        if (
            result.id != source.id
            or result.run_id != scope.run_id
            or result.tool_call_id != scope.tool_call_id
            or result.evidence_epoch != scope.evidence_epoch
            or result.content_hash != source.content_hash
            or result.byte_count != len(source.text.encode("utf-8"))
            or result.line_count != (len(source.text.split("\n")) if source.text else 0)
            or result.status != source.status
            or result.requested_url != source.requested_url
            or result.final_url != source.final_url
            or result.canonical_url != source.canonical_url
            or result.title != source.title
            or result.publisher != source.publisher
            or result.published_at != source.published_at
            or result.extractor_version != source.extractor_version
            or result.error_code != source.error_code
            or result.fetched_at is None
            or datetime.fromisoformat(result.fetched_at.replace("Z", "+00:00"))
            != datetime.fromisoformat(source.fetched_at.replace("Z", "+00:00"))
        ):
            raise RuntimeError("Supabase changed the recorded source identity or content")
        return result

    async def list_web_sources(
        self, session_id: str, offset: int = 0, limit: int = 20
    ) -> SourcePage:
        row = _object(
            await self._web_call(
                "list_sources", {"session_id": session_id, "offset": offset, "limit": limit}
            )
        )
        items = tuple(self._source(item, session_id) for item in _rows(row.get("items"), limit))
        next_offset = (
            None if row.get("next_offset") is None else _integer(row, "next_offset", 1_000_000)
        )
        if next_offset is not None and (next_offset != offset + len(items) or not items):
            raise RuntimeError("Supabase returned an invalid source page boundary")
        return SourcePage(items, next_offset)

    async def read_web_source(
        self, session_id: str, source_id: str, start_line: int = 1, max_lines: int = 100
    ) -> SourceLines | None:
        value = await self._web_call(
            "read_source",
            {
                "session_id": session_id,
                "source_id": source_id,
                "start_line": start_line,
                "max_lines": max_lines,
            },
        )
        if value is None:
            return None
        row = _object(value)
        source = self._source(row.get("source"), session_id)
        lines = row.get("lines")
        if (
            not isinstance(lines, list)
            or len(lines) > max_lines
            or not all(isinstance(line, str) for line in lines)
        ):
            raise RuntimeError("Supabase returned invalid source lines")
        result = SourceLines(
            source, _integer(row, "start_line"), tuple(lines), _integer(row, "total_lines")
        )
        if (
            result.source.id != source_id
            or result.start_line != start_line
            or result.total_lines != source.line_count
            or len("\n".join(result.lines).encode("utf-8")) > MAX_SOURCE_READ_BYTES
            or (result.lines and result.start_line + len(result.lines) - 1 > result.total_lines)
        ):
            raise RuntimeError("Supabase returned an invalid source line range")
        return result

    async def clear_web_sources(self, session_id: str) -> ClearSourcesResult:
        row = _object(await self._web_call("clear_sources", {"session_id": session_id}))
        return ClearSourcesResult(_integer(row, "evidence_epoch"), _integer(row, "cleared_count"))

    def _crawl(self, value: object, session_id: str) -> WebCrawl:
        row = _object(value)
        result = WebCrawl(
            _string(row, "id"),
            _string(row, "user_id"),
            _string(row, "session_id"),
            _string(row, "run_id"),
            _string(row, "tool_call_id"),
            _integer(row, "evidence_epoch"),
            _string(row, "seed_url"),
            _string(row, "origin"),
            _string(row, "path_prefix"),
            _integer(row, "max_pages", 20),
            _integer(row, "max_depth", 2),
            _string(row, "status"),
            _optional_string(row, "stop_reason"),
            _string(row, "created_at"),
            _string(row, "updated_at"),
        )
        if (
            result.user_id != str(self._user_id)
            or result.session_id != session_id
            or result.status not in {"running", "completed", "stopped"}
        ):
            raise RuntimeError("Supabase returned an invalid crawl scope")
        return result

    @staticmethod
    def _page(value: object, crawl_id: str) -> CrawlPage:
        row = _object(value)
        result = CrawlPage(
            _string(row, "crawl_id"),
            _string(row, "url"),
            _integer(row, "depth", 2),
            _string(row, "status"),
            _integer(row, "attempt", 4),
            None if row.get("generation") is None else _integer(row, "generation"),
            _optional_string(row, "source_id"),
        )
        if result.crawl_id != crawl_id or result.status not in {"queued", "fetching", "completed"}:
            raise RuntimeError("Supabase returned an invalid crawl page")
        return result

    async def create_web_crawl(
        self,
        scope: WebScope,
        crawl_id: str,
        seed_url: str,
        origin: str,
        path_prefix: str,
        max_pages: int,
        max_depth: int,
    ) -> WebCrawl:
        result = self._crawl(
            await self._web_call(
                "create_crawl",
                {
                    **self._scope_payload(scope),
                    "crawl_id": crawl_id,
                    "seed_url": seed_url,
                    "origin": origin,
                    "path_prefix": path_prefix,
                    "max_pages": max_pages,
                    "max_depth": max_depth,
                },
                run_id=scope.run_id,
            ),
            scope.session_id,
        )
        if (
            result.id != crawl_id
            or result.run_id != scope.run_id
            or result.tool_call_id != scope.tool_call_id
        ):
            raise RuntimeError("Supabase returned another operation's crawl")
        return result

    async def get_web_crawl(self, session_id: str, crawl_id: str) -> WebCrawl | None:
        value = await self._web_call("get_crawl", {"session_id": session_id, "crawl_id": crawl_id})
        result = None if value is None else self._crawl(value, session_id)
        if result is not None and result.id != crawl_id:
            raise RuntimeError("Supabase returned another crawl")
        return result

    async def enqueue_web_pages(
        self, scope: WebScope, crawl_id: str, pages: tuple[tuple[str, int], ...]
    ) -> int:
        row = _object(
            await self._web_call(
                "enqueue_pages",
                {
                    **self._scope_payload(scope),
                    "crawl_id": crawl_id,
                    "pages": [{"url": url, "depth": depth} for url, depth in pages],
                },
                run_id=scope.run_id,
            )
        )
        return _integer(row, "enqueued_count", 200)

    async def claim_web_page(self, scope: WebScope, crawl_id: str) -> CrawlPage | None:
        value = await self._web_call(
            "claim_page", {**self._scope_payload(scope), "crawl_id": crawl_id}, run_id=scope.run_id
        )
        return None if value is None else self._page(value, crawl_id)

    async def complete_web_page(
        self, scope: WebScope, crawl_id: str, url: str, source_id: str
    ) -> CrawlPage:
        result = self._page(
            await self._web_call(
                "complete_page",
                {
                    **self._scope_payload(scope),
                    "crawl_id": crawl_id,
                    "url": url,
                    "source_id": source_id,
                },
                run_id=scope.run_id,
            ),
            crawl_id,
        )
        if result.url != url or result.source_id != source_id or result.status != "completed":
            raise RuntimeError("Supabase changed the completed crawl page")
        return result

    async def list_web_pages(
        self, session_id: str, crawl_id: str, offset: int = 0, limit: int = 200
    ) -> CrawlPageBatch:
        value = await self._web_call(
            "list_pages",
            {"session_id": session_id, "crawl_id": crawl_id, "offset": offset, "limit": limit},
        )
        if value is None:
            return CrawlPageBatch((), None)
        row = _object(value)
        items = tuple(self._page(item, crawl_id) for item in _rows(row.get("items"), limit))
        next_offset = None if row.get("next_offset") is None else _integer(row, "next_offset", 200)
        if next_offset is not None and next_offset != offset + len(items):
            raise RuntimeError("Supabase returned an invalid frontier page boundary")
        return CrawlPageBatch(items, next_offset)

    async def checkpoint_web_crawl(
        self, scope: WebScope, crawl_id: str, status: str, stop_reason: str | None = None
    ) -> WebCrawl:
        return self._crawl(
            await self._web_call(
                "checkpoint_crawl",
                {
                    **self._scope_payload(scope),
                    "crawl_id": crawl_id,
                    "status": status,
                    "stop_reason": stop_reason,
                },
                run_id=scope.run_id,
            ),
            scope.session_id,
        )

    @staticmethod
    def _bindings(value: object, model_call_id: str) -> tuple[CitationBinding, ...]:
        result = tuple(
            CitationBinding(
                _string(row, "model_call_id"),
                _string(row, "source_id"),
                _integer(row, "start_line"),
                _integer(row, "end_line"),
                _integer(row, "citation_index", 50),
            )
            for row in _rows(value, 50)
        )
        if any(
            item.model_call_id != model_call_id or item.citation_index != index
            for index, item in enumerate(result, 1)
        ):
            raise RuntimeError("Supabase returned another answer's citations")
        for item in result:
            CitationReference(item.source_id, item.start_line, item.end_line)
        return result

    async def bind_web_citations(
        self,
        run_id: str,
        generation: int,
        model_call_id: str,
        citations: tuple[CitationReference, ...],
    ) -> tuple[CitationBinding, ...]:
        result = self._bindings(
            await self._web_call(
                "bind_citations",
                {
                    "run_id": run_id,
                    "generation": generation,
                    "model_call_id": model_call_id,
                    "citations": [item.to_dict() for item in citations],
                },
                run_id=run_id,
            ),
            model_call_id,
        )
        if (
            tuple(
                CitationReference(item.source_id, item.start_line, item.end_line) for item in result
            )
            != citations
        ):
            raise RuntimeError("Supabase changed the answer's citations")
        return result

    async def read_web_citations(
        self, session_id: str, model_call_id: str
    ) -> tuple[CitationBinding, ...]:
        return self._bindings(
            await self._web_call(
                "read_citations", {"session_id": session_id, "model_call_id": model_call_id}
            ),
            model_call_id,
        )
