import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from run_fixtures import Provider, Repository, service
from web_fixtures import DISPATCH, OWNER, RUN, SESSION, SOURCE, TOOL

from app.api.dependencies import get_web_evidence_service
from app.api.routes.sources import router as source_router
from app.application.budgets import RunLimits
from app.application.context import build_model_context
from app.application.errors import ApplicationError
from app.application.secret_redaction import SecretRedactor
from app.application.tool_context import ToolExecutionContext
from app.application.tools import MAX_RESULT_BYTES, ToolRegistry
from app.application.web_evidence import WebEvidenceService
from app.application.web_ports import WebRepository
from app.application.web_tools import WebToolService
from app.core.config import Settings
from app.domain.capabilities import RuntimeCapabilities
from app.domain.models import Session
from app.domain.recovery import ToolRecoveryPolicy
from app.domain.retrieval import ExtractedDocumentResult
from app.domain.runtime import (
    ModelToolCall,
    RunEventType,
    RunMessageRecord,
    RunStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from app.domain.search import SearchHit, SearchResult, SearchStatus
from app.domain.web import (
    MAX_SOURCE_BYTES,
    SourceLines,
    SourceStatus,
    WebReservation,
    WebRunState,
    WebSource,
    WebUsage,
    canonical_source_text,
)
from app.infrastructure.accounts import AccountRegistry
from app.infrastructure.runtime_events import RuntimeEventHub

CAPABILITIES = RuntimeCapabilities(frozenset({"web_search", "web_read"}))
KEY = "brave-fixture-credential-which-is-private"


def context(**changes):
    return replace(
        ToolExecutionContext(
            OWNER,
            SESSION,
            RUN,
            TOOL,
            DISPATCH,
            1,
            2,
            datetime.now(UTC) + timedelta(minutes=2),
            RunLimits(10, 20, 100000, 120, 10),
            None,
            AsyncMock(),
        ),
        **changes,
    )


class EvidenceRepository:
    def __init__(self):
        self.sources = {}
        self.reservations = {}
        self.usage = WebUsage()
        self.operations = []
        self.monthly_denied = False

    async def open_web_run(self, dispatch):
        self.operations.append("open")
        return WebRunState(0, self.usage)

    async def reserve_web_budget(self, scope, reservation_id, operation_key, usage):
        if (
            self.usage.search_calls + usage.search_calls > 5
            or self.usage.page_attempts + usage.page_attempts > 20
        ):
            raise ApplicationError("web_budget_exhausted", "Allowance reached.")
        if self.usage.response_bytes + usage.response_bytes > 64 * 1024 * 1024:
            raise ApplicationError("web_budget_exhausted", "Allowance reached.")
        reservation = WebReservation(reservation_id, scope, operation_key, usage, None, "reserved")
        self.reservations[reservation_id] = reservation
        self.usage = WebUsage(
            **{name: getattr(self.usage, name) + value for name, value in usage.to_dict().items()}
        )
        self.operations.append("reserve")
        return reservation

    async def reserve_search_attempt(self, scope, request_id, key_hash, monthly_limit):
        assert request_id in self.reservations
        assert key_hash == hashlib.sha256(KEY.encode()).hexdigest()
        assert monthly_limit <= 1000
        self.operations.append("meter")
        if self.monthly_denied:
            raise ApplicationError("web_search_budget_exhausted", "Search limit reached.")

    async def settle_web_budget(self, scope, reservation_id, usage):
        reservation = self.reservations[reservation_id]
        assert all(
            value <= reservation.reserved.to_dict()[key] for key, value in usage.to_dict().items()
        )
        self.usage = WebUsage(
            **{
                name: getattr(self.usage, name) - value + getattr(usage, name)
                for name, value in reservation.reserved.to_dict().items()
            }
        )
        settled = replace(reservation, actual=usage, status="settled")
        self.reservations[reservation_id] = settled
        self.operations.append("settle")
        return settled

    async def record_web_source(self, scope, reservation_id, draft):
        assert reservation_id in self.reservations
        self.sources[draft.id] = (scope, draft)
        self.operations.append("record")
        return self._lines(draft.id, 1, 3).source

    def _lines(self, source_id, start, limit):
        scope, draft = self.sources[source_id]
        row = {
            **asdict(draft),
            "user_id": scope.user_id,
            "session_id": scope.session_id,
            "run_id": scope.run_id,
            "tool_call_id": scope.tool_call_id,
            "evidence_epoch": scope.evidence_epoch,
            "content_hash": draft.content_hash,
            "byte_count": len(draft.text.encode()),
            "line_count": len(draft.text.split("\n")) if draft.text else 0,
            "created_at": draft.fetched_at,
        }
        lines = tuple(draft.text.split("\n")) if draft.text else ()
        return SourceLines(
            WebSource.from_dict(row), start, lines[start - 1 : start - 1 + limit], len(lines)
        )

    async def read_web_source(self, session_id, source_id, start_line=1, max_lines=100):
        if source_id not in self.sources or self.sources[source_id][0].session_id != session_id:
            return None
        return self._lines(source_id, start_line, max_lines)


def tools(
    repository=None, *, reader=None, renderer=None, search=None, redactor=None, progress=None
):
    repository = repository or EvidenceRepository()
    sessions = Mock(
        get_session=AsyncMock(return_value=Session(SESSION, OWNER, "title", "now", "now", 0))
    )
    evidence = WebEvidenceService(cast(WebRepository, repository), sessions, owner_id=OWNER)
    executor = WebToolService(
        evidence,
        retriever=reader,
        renderer=renderer,
        search=search,
        search_key_hash=hashlib.sha256(KEY.encode()).hexdigest() if search is not None else None,
        redactor=redactor or SecretRedactor(),
        publish_progress=progress,
    )
    registry = ToolRegistry(
        capabilities=CAPABILITIES,
        additional_tools=executor.registrations(
            search_ready=True, read_ready=True, render_ready=renderer is not None
        ),
        redactor=redactor,
    )
    return repository, executor, registry


def reader(text="First supporting fact.\nSecond supporting fact.", **metadata):
    async def read(request, trusted_context):
        await trusted_context.assert_active()
        return ExtractedDocumentResult(
            status=SourceStatus.SUCCESS,
            requested_url=request.url,
            final_url=request.url,
            media_type="text/html",
            text=text,
            usage=WebUsage(
                page_attempts=1,
                network_requests=1,
                response_bytes=1000,
                extracted_bytes=len(text.encode()),
            ),
            **metadata,
        )

    return Mock(read_document=AsyncMock(side_effect=read))


def test_web_registry_has_narrow_strict_arguments_and_no_workspace_access():
    async def exercise():
        _, _, registry = tools()
        assert {item.name for item in registry.specs(None)} == {
            "web_search",
            "web_read",
            "web_source",
        }
        for name in ("web_search", "web_read", "web_source"):
            assert registry.recovery_policy(name) is ToolRecoveryPolicy.CHECKPOINTED
            assert not registry.requires_workspace(name)
        cases: list[tuple[str, dict[str, object]]] = [
            ("web_search", {"query": "example", "user_id": OWNER}),
            ("web_search", {"query": "example", "max_results": True}),
            ("web_search", {"query": "private\nconversation"}),
            ("web_search", {"query": "example", "max_results": 11}),
            ("web_read", {"urls": ["https://example.com"] * 6}),
            ("web_source", {"source_id": SOURCE, "max_lines": 201}),
            ("web_source", {"source_id": "invalid"}),
        ]
        for name, arguments in cases:
            result = await registry.execute(
                ModelToolCall("call", name, arguments), None, context=context()
            )
            assert result.error_code == "invalid_tool_arguments"

    asyncio.run(exercise())


def test_render_is_advertised_only_after_readiness_and_accepts_only_a_boolean():
    async def exercise():
        _, _, plain = tools()
        specification = next(item for item in plain.specs(None) if item.name == "web_read")
        assert "render" not in specification.input_schema["properties"]
        renderer = Mock(render_document=reader().read_document)
        _, _, rendered = tools(renderer=renderer)
        specification = next(item for item in rendered.specs(None) if item.name == "web_read")
        assert specification.input_schema["properties"]["render"]["default"] is False
        assert "render=True" in specification.description
        for value in ("true", 1, None):
            result = await rendered.execute(
                ModelToolCall(
                    "call", "web_read", {"urls": ["https://example.com"], "render": value}
                ),
                None,
                context=context(),
            )
            assert result.error_code == "invalid_tool_arguments"
        renderer.render_document.assert_not_awaited()

    asyncio.run(exercise())


def test_render_reserves_one_page_and_bounded_browser_allowance_before_dispatch():
    async def exercise():
        raw = reader()
        renderer = Mock(render_document=reader().read_document)
        repository, _, registry = tools(reader=raw, renderer=renderer)
        trusted = context(deadline_at=datetime.now(UTC) + timedelta(seconds=45))
        result = await registry.execute(
            ModelToolCall("call", "web_read", {"urls": ["https://example.com"], "render": True}),
            None,
            context=trusted,
        )
        assert json.loads(result.content)["pages"][0]["status"] == "success"
        raw.read_document.assert_not_awaited()
        request, passed_context = renderer.render_document.await_args.args
        assert type(request).__name__ == "RenderRequest"
        assert passed_context is trusted
        assert 0 < request.timeout_seconds <= 45
        assert request.max_requests == 75
        assert request.max_response_bytes == 18 * 1024 * 1024
        reservation = next(iter(repository.reservations.values()))
        assert reservation.reserved.page_attempts == 1
        assert reservation.reserved.network_requests == 75
        assert repository.operations.index("reserve") < repository.operations.index("record")

    asyncio.run(exercise())


def test_unavailable_renderer_skips_io_and_reuses_completed_render_sources():
    async def exercise():
        raw = reader()
        renderer = Mock(render_document=reader().read_document)
        repository, executor, _ = tools(reader=raw, renderer=renderer)
        arguments = {"urls": ["https://example.com"], "render": True}
        content, _ = await executor.execute("web_read", arguments, context())
        source_id = json.loads(content)["pages"][0]["source_id"]
        executor._renderer = None
        content, _ = await executor.execute(
            "web_read", arguments, context(dispatch_attempt=2, generation=3)
        )
        page = json.loads(content)["pages"][0]
        assert page["source_id"] == source_id
        assert page["reused"]
        assert renderer.render_document.await_count == 1
        raw.read_document.assert_not_awaited()
        calls = len(repository.reservations)
        content, _ = await executor.execute(
            "web_read", arguments, context(tool_call_id=str(uuid4()))
        )
        page = json.loads(content)["pages"][0]
        assert page["status"] == "unavailable"
        assert page["error_code"] == "renderer_not_configured"
        assert page["not_attempted"]
        assert len(repository.reservations) == calls
        raw.read_document.assert_not_awaited()

    asyncio.run(exercise())


def test_unavailable_render_schema_keeps_checkpointed_arguments_executable():
    async def exercise():
        renderer = Mock(render_document=reader().read_document)
        repository, executor, registry = tools(renderer=renderer)
        call = ModelToolCall("call", "web_read", {"urls": ["https://example.com"], "render": True})
        first = await registry.execute(call, None, context=context())
        source = json.loads(first.content)["pages"][0]["source_id"]
        executor._renderer = None
        unavailable = ToolRegistry(
            None,
            capabilities=CAPABILITIES,
            additional_tools=executor.registrations(search_ready=False, read_ready=True),
        )
        specification = next(item for item in unavailable.specs(None) if item.name == "web_read")
        properties = specification.input_schema["properties"]
        assert isinstance(properties, dict) and "render" not in properties
        resumed = await unavailable.execute(
            call, None, context=context(dispatch_attempt=2, generation=3)
        )
        assert not resumed.is_error
        page = json.loads(resumed.content)["pages"][0]
        assert page["source_id"] == source and page["reused"]
        assert renderer.render_document.await_count == 1
        assert len(repository.reservations) == 1

    asyncio.run(exercise())


def test_read_records_accepted_redacted_source_before_settlement():
    async def exercise():
        text = f"Original evidence {KEY}.\nA second supporting line."
        progress = AsyncMock()
        repository, _, registry = tools(
            reader=reader(text), redactor=SecretRedactor(KEY), progress=progress
        )
        result = await registry.execute(
            ModelToolCall("call", "web_read", {"urls": ["https://example.com"]}),
            None,
            context=context(),
        )
        value = json.loads(result.content)
        source = value["pages"][0]
        accepted = text.replace(KEY, "[REDACTED]")
        assert source["status"] == "partial"
        assert source["error_code"] == "sensitive_content_redacted"
        assert source["display_redacted"]
        assert KEY not in result.content
        assert source["content_hash"] == hashlib.sha256(accepted.encode()).hexdigest()
        assert source["untrusted_excerpt"][0]["line"] == 1
        assert repository.sources[source["source_id"]][1].text == accepted
        assert (
            repository.operations.index("reserve")
            < repository.operations.index("record")
            < repository.operations.index("settle")
        )
        assert repository.usage.extracted_bytes == len(accepted.encode())
        assert all(
            KEY not in str(call) and "https://" not in str(call)
            for call in progress.await_args_list
        )
        assert all(
            reservation.reserved.response_bytes == 18 * 1024 * 1024
            for reservation in repository.reservations.values()
        )

    asyncio.run(exercise())


def test_wrapped_secret_is_absent_from_persisted_source_and_every_paginated_view():
    async def exercise():
        text = canonical_source_text("A" * 2040 + KEY + " trailing text\nLast fact.")
        repository, executor, registry = tools(reader=reader(text), redactor=SecretRedactor(KEY))
        result = await registry.execute(
            ModelToolCall("call", "web_read", {"urls": ["https://example.com"]}),
            None,
            context=context(),
        )
        summary = json.loads(result.content)["pages"][0]
        draft = repository.sources[summary["source_id"]][1]
        assert KEY not in draft.text.replace("\n", "")
        assert "brave-fi" not in draft.text and "xture-credential" not in draft.text
        assert "[REDACTED]" in draft.text.replace("\n", "")
        assert draft.status is SourceStatus.PARTIAL
        assert draft.error_code == "sensitive_content_redacted"
        assert summary["display_redacted"]
        assert KEY not in result.content.replace("\\n", "")
        assert summary["content_hash"] == hashlib.sha256(draft.text.encode()).hexdigest()

        app = FastAPI()
        app.include_router(source_router)
        app.dependency_overrides[get_web_evidence_service] = lambda: executor._evidence
        returned = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for line in range(1, summary["total_lines"] + 1):
                output = await registry.execute(
                    ModelToolCall(
                        "call",
                        "web_source",
                        {"source_id": draft.id, "start_line": line, "max_lines": 1},
                    ),
                    None,
                    context=context(tool_call_id=str(uuid4())),
                )
                page = json.loads(output.content)
                assert page["display_redacted"]
                assert page["error_code"] == "sensitive_content_redacted"
                returned.extend(item["text"] for item in page["untrusted_lines"])
                response = await client.get(
                    f"/api/sessions/{SESSION}/sources/{draft.id}",
                    params={"start_line": line, "max_lines": 1},
                )
                assert response.status_code == 200
                assert response.json()["lines"] == [draft.text.split("\n")[line - 1]]
                assert KEY not in response.text.replace("\\n", "")
                assert response.json()["source"]["content_hash"] == draft.content_hash
        assert "\n".join(returned) == draft.text

    asyncio.run(exercise())


def test_clean_source_keeps_original_hash_and_metadata():
    async def exercise():
        text = canonical_source_text("A" * 2050 + "\nOriginal supporting fact.")
        repository, _, registry = tools(reader=reader(text, title="A clean title"))
        result = await registry.execute(
            ModelToolCall("call", "web_read", {"urls": ["https://example.com"]}),
            None,
            context=context(),
        )
        summary = json.loads(result.content)["pages"][0]
        draft = repository.sources[summary["source_id"]][1]
        assert draft.text == text
        assert draft.status is SourceStatus.SUCCESS and draft.error_code is None
        assert draft.title == "A clean title"
        assert summary["content_hash"] == hashlib.sha256(text.encode()).hexdigest()
        assert not summary["display_redacted"]

    asyncio.run(exercise())


def test_source_filter_reapplies_text_and_metadata_limits_after_mask_expansion():
    async def exercise():
        text = canonical_source_text("z" * 260_000)
        repository, _, registry = tools(
            reader=reader(text, title="z" * 1000, publisher="z" * 500),
            redactor=SecretRedactor("z"),
        )
        result = await registry.execute(
            ModelToolCall("call", "web_read", {"urls": ["https://example.com"]}),
            None,
            context=context(),
        )
        summary = json.loads(result.content)["pages"][0]
        draft = repository.sources[summary["source_id"]][1]
        assert len(draft.text.encode()) == MAX_SOURCE_BYTES
        assert draft.text == canonical_source_text(draft.text)
        assert len(draft.title) == 1000 and len(draft.publisher) == 500
        assert "z" not in str(asdict(draft))
        assert repository.usage.extracted_bytes == MAX_SOURCE_BYTES
        assert draft.status is SourceStatus.PARTIAL

    asyncio.run(exercise())


def test_secret_bearing_metadata_and_blocked_url_are_masked_before_storage_and_recovery():
    async def exercise():
        url = f"https://example.com/{KEY}"
        retriever = reader()
        repository, _, registry = tools(reader=retriever, redactor=SecretRedactor(KEY))
        call = ModelToolCall("call", "web_read", {"urls": [url]})
        first = json.loads((await registry.execute(call, None, context=context())).content)
        draft = repository.sources[first["pages"][0]["source_id"]][1]
        assert draft.requested_url == "https://example.com/[REDACTED]"
        assert draft.status is SourceStatus.BLOCKED and draft.error_code == "sensitive_url"
        recovered = json.loads(
            (await registry.execute(call, None, context=context(dispatch_attempt=2))).content
        )
        assert recovered["pages"][0]["reused"]
        retriever.read_document.assert_not_awaited()

        document = ExtractedDocumentResult(
            status=SourceStatus.SUCCESS,
            requested_url="https://example.com",
            final_url=url,
            canonical_url=url,
            media_type="text/plain",
            text="Safe source text.",
            title=KEY[:8] + "\n" + KEY[8:],
            publisher=KEY,
            published_at=KEY,
            extractor_version=KEY,
            usage=WebUsage(page_attempts=1, extracted_bytes=17),
        )
        repository, _, registry = tools(
            reader=Mock(read_document=AsyncMock(return_value=document)),
            redactor=SecretRedactor(KEY),
        )
        result = await registry.execute(
            ModelToolCall("call", "web_read", {"urls": ["https://example.com"]}),
            None,
            context=context(),
        )
        summary = json.loads(result.content)["pages"][0]
        draft = repository.sources[summary["source_id"]][1]
        assert KEY not in str(asdict(draft)).replace("\\n", "")
        assert draft.title == "[REDACTED]"
        assert draft.status is SourceStatus.PARTIAL
        assert draft.error_code == "sensitive_content_redacted"
        assert summary["display_redacted"]

    asyncio.run(exercise())


def test_recovery_reuses_confirmed_sources_without_repeating_http_or_renewing_budgets():
    async def exercise():
        retriever = reader()
        repository, _, registry = tools(reader=retriever)
        call = ModelToolCall("call", "web_read", {"urls": ["https://example.com/a"]})
        first = json.loads((await registry.execute(call, None, context=context())).content)
        usage = repository.usage
        retried = replace(
            call, arguments={"urls": ["https://example.com/a", "https://example.com/b"]}
        )
        recovered = json.loads(
            (
                await registry.execute(
                    retried,
                    None,
                    context=context(dispatch_attempt=2, generation=3, dispatch_id=str(uuid4())),
                )
            ).content
        )
        assert recovered["pages"][0]["source_id"] == first["pages"][0]["source_id"]
        assert recovered["pages"][0]["reused"]
        assert recovered["pages"][1]["error_code"] == "read_not_replayed"
        assert retriever.read_document.await_count == 1
        assert repository.usage == usage

    asyncio.run(exercise())


def test_shared_page_allowance_prevents_dispatch_and_failure_sources_are_not_citable():
    async def exercise():
        retriever = Mock(
            read_document=AsyncMock(
                return_value=ExtractedDocumentResult(
                    status=SourceStatus.UNSUPPORTED,
                    requested_url="https://example.com/scanned.pdf",
                    error_code="pdf_ocr_required",
                    usage=WebUsage(page_attempts=1),
                )
            )
        )
        repository, _, registry = tools(reader=retriever)
        call = ModelToolCall("call", "web_read", {"urls": ["https://example.com/scanned.pdf"]})
        value = json.loads((await registry.execute(call, None, context=context())).content)
        assert value["pages"][0]["status"] == "unsupported"
        assert value["pages"][0]["total_lines"] == 0
        assert repository.sources[value["pages"][0]["source_id"]][1].text == ""
        repository.usage = replace(repository.usage, page_attempts=20)
        value = json.loads(
            (await registry.execute(call, None, context=context(tool_call_id=str(uuid4())))).content
        )
        assert value["pages"][0]["status"] == "budget_exhausted"
        assert retriever.read_document.await_count == 1

    asyncio.run(exercise())


def test_search_reserves_run_and_shared_key_attempt_before_provider_and_never_becomes_a_source():
    async def exercise():
        repository = EvidenceRepository()

        async def search(query, count, trusted_context):
            assert repository.operations[-1] == "meter"
            assert query == "public example"
            return SearchResult(
                SearchStatus.SUCCESS,
                (SearchHit("Example", "https://example.com", "Candidate snippet."),),
                response_bytes=200,
            )

        provider = Mock(search=AsyncMock(side_effect=search))
        _, _, registry = tools(repository, search=provider)
        value = json.loads(
            (
                await registry.execute(
                    ModelToolCall("call", "web_search", {"query": "public example"}),
                    None,
                    context=context(),
                )
            ).content
        )
        assert value["not_read"] and value["untrusted_discovery"]
        assert not repository.sources
        assert repository.usage.search_calls == repository.usage.network_requests == 1
        assert repository.usage.response_bytes == 200
        recovered = json.loads(
            (
                await registry.execute(
                    ModelToolCall("call", "web_search", {"query": "public example"}),
                    None,
                    context=context(dispatch_attempt=2),
                )
            ).content
        )
        assert recovered["error_code"] == "search_not_replayed"
        assert provider.search.await_count == 1

    asyncio.run(exercise())


def test_missing_key_and_monthly_limit_never_contact_provider():
    async def exercise():
        _, _, registry = tools()
        call = ModelToolCall("call", "web_search", {"query": "example"})
        assert (
            json.loads((await registry.execute(call, None, context=context())).content)[
                "error_code"
            ]
            == "search_not_configured"
        )
        repository = EvidenceRepository()
        repository.monthly_denied = True
        provider = Mock(search=AsyncMock())
        _, _, registry = tools(repository, search=provider)
        assert (
            json.loads((await registry.execute(call, None, context=context())).content)[
                "error_code"
            ]
            == "web_search_budget_exhausted"
        )
        provider.search.assert_not_awaited()

    asyncio.run(exercise())


def test_untrusted_quotes_unicode_and_long_urls_stay_valid_json_under_tool_ceiling():
    async def exercise():
        text = (('token=x"quoted\\tail ' + "🪴" * 450 + "\n") * 100).strip()
        long_url = "https://example.com/" + "u" * 3200
        repository, _, registry = tools(reader=reader(text, title='title " 🪴' * 90))
        value = await registry.execute(
            ModelToolCall("call", "web_read", {"urls": [long_url] * 5}), None, context=context()
        )
        decoded = json.loads(value.content)
        assert len(value.content.encode()) < MAX_RESULT_BYTES
        assert len(decoded["pages"]) == 5
        assert all(
            page["final_url"] is None and page["final_url_available_in_source"]
            for page in decoded["pages"]
        )
        source = decoded["pages"][0]["source_id"]
        value = await registry.execute(
            ModelToolCall("call", "web_source", {"source_id": source, "max_lines": 200}),
            None,
            context=context(tool_call_id=str(uuid4())),
        )
        decoded = json.loads(value.content)
        assert len(value.content.encode()) < MAX_RESULT_BYTES
        assert decoded["has_more"]
        assert decoded["untrusted_lines"][0]["line"] == 1
        assert decoded["display_redacted"]
        assert repository.sources[source][1].requested_url == long_url

    asyncio.run(exercise())


def test_context_reconstruction_preserves_escaped_json_tool_results():
    content = json.dumps({"untrusted": 'token=x"quoted\\tail 🪴'})
    tool = ToolCallRecord(
        TOOL,
        RUN,
        "assistant",
        0,
        "provider-call",
        "web_source",
        {},
        ToolCallStatus.COMPLETED,
        None,
        None,
        "now",
        "now",
    )
    message = RunMessageRecord("message", RUN, 1, "tool", content, None, TOOL, "now")
    rebuilt = build_model_context(
        (), (message,), workspace_root=None, tools=(), run_tool_calls=(tool,)
    )
    assert json.loads(rebuilt.messages[0].content)["untrusted"].startswith("token=[REDACTED]")
    assert "Search discovers candidate pages" in rebuilt.system_instructions
    assert "[[source:<source-id>:L1-L3]]" in rebuilt.system_instructions


def test_account_web_registration_fails_closed_and_separates_search_from_reader(monkeypatch):
    async def exercise():
        settings = Settings(
            _env_file=None,
            enabled_capabilities={"web_read", "web_search"},
            brave_search_api_key=KEY,
            web_gateway_admin_secret="gateway-private-fixture-credential",
        )
        registry = AccountRegistry(settings, {}, {})
        repository = Mock(web_tools_ready=AsyncMock(return_value=False))
        evidence = Mock()
        assert await registry._web_tool_registrations(repository, evidence, RuntimeEventHub()) == ()
        repository.web_tools_ready.return_value = True
        evidence.repository = repository
        registrations = await registry._web_tool_registrations(
            repository, evidence, RuntimeEventHub()
        )
        assert {tool.name for tool in registrations} == {"web_source", "web_search"}
        assert KEY not in registry._redactor.redact_text(KEY)
        assert "gateway-private-fixture-credential" not in registry._redactor.redact_text(
            "gateway-private-fixture-credential"
        )
        registry._settings = settings.model_copy(update={"brave_search_api_key": None})
        assert {
            tool.name
            for tool in await registry._web_tool_registrations(
                repository, evidence, RuntimeEventHub()
            )
        } == {"web_source"}
        assert not registry._capabilities.workspace_enabled

    asyncio.run(exercise())


def test_normal_run_service_dispatches_search_read_source_without_a_workspace():
    class RuntimeRepository(Repository, EvidenceRepository):
        def __init__(self):
            Repository.__init__(self)
            EvidenceRepository.__init__(self)
            self.run = replace(
                self.run, id=RUN, session_id=SESSION, status=RunStatus.RUNNING, generation=2
            )
            self.tool: ToolCallRecord | None = None
            self.results = []

        async def list_tool_calls(self, _run_id):
            return [self.tool] if self.tool else []

        async def begin_tool_execution(self, run_id, tool_id, **dispatch):
            assert self.tool is not None
            self.tool = replace(
                self.tool,
                status=ToolCallStatus.RUNNING,
                dispatch_id=dispatch["dispatch_id"],
                dispatch_attempt=1,
                dispatch_generation=self.run.generation,
                recovery_policy=dispatch["recovery_policy"],
                input_snapshot=dispatch["input_snapshot"],
            )
            return self.tool, await self.append_run_event(run_id, RunEventType.TOOL_CALL, {})

        async def record_tool_result(self, run_id, tool_id, content, **kwargs):
            assert self.tool is not None
            self.tool = replace(self.tool, status=kwargs["status"])
            self.results.append(json.loads(content))
            assert kwargs["result_snapshot"]["source"] == "checkpointed_web"
            return (
                None,
                self.tool,
                await self.append_run_event(run_id, RunEventType.TOOL_RESULT, {}),
            )

    async def exercise():
        repository = RuntimeRepository()
        provider = Mock(
            search=AsyncMock(
                return_value=SearchResult(
                    SearchStatus.SUCCESS,
                    (SearchHit("Fixture", "https://example.com", "Candidate only."),),
                )
            )
        )
        _, _, registry = tools(
            repository,
            reader=reader('token=x"quoted\\tail 🪴\nA supporting fact.'),
            search=provider,
        )
        runtime = service(repository, Provider(), owner_id=OWNER)
        runtime._sessions = Mock(
            get_session=AsyncMock(return_value=Session(SESSION, OWNER, "title", "now", "now", 0))
        )
        profile = await runtime._profiles.get_profile()
        assert profile is not None
        runtime._profiles = Mock(get_profile=AsyncMock(return_value=replace(profile, id=OWNER)))
        runtime._tool_registry = registry
        runtime._capabilities = CAPABILITIES
        runtime._owned_runs.add(RUN)
        runtime._workspace_bindings = Mock(
            resolve=AsyncMock(side_effect=AssertionError("Workspace accessed"))
        )
        execution = await runtime._execution_context(repository.run)
        assert execution.workspace_root is None
        calls: list[tuple[str, dict[str, object]]] = [
            ("web_search", {"query": "fixture"}),
            ("web_read", {"urls": ["https://example.com"]}),
        ]
        for name, arguments in calls:
            repository.tool = ToolCallRecord(
                str(uuid4()),
                RUN,
                "assistant",
                0,
                "provider-tool",
                name,
                arguments,
                ToolCallStatus.PENDING,
                None,
                None,
                "now",
                None,
            )
            assert await runtime._execute_pending_tools(repository.run, execution)
        source_id = repository.results[-1]["pages"][0]["source_id"]
        repository.tool = ToolCallRecord(
            str(uuid4()),
            RUN,
            "assistant",
            0,
            "provider-tool",
            "web_source",
            {"source_id": source_id, "max_lines": 2},
            ToolCallStatus.PENDING,
            None,
            None,
            "now",
            None,
        )
        assert await runtime._execute_pending_tools(repository.run, execution)
        assert repository.results[0]["not_read"]
        assert repository.results[-1]["untrusted_lines"][1]["text"] == "A supporting fact."
        assert repository.tool.recovery_policy == "checkpointed"
        runtime._workspace_bindings.resolve.assert_not_awaited()

    asyncio.run(exercise())


def test_storage_quota_records_attempted_failure_and_settles_network_usage():
    class FullEvidence(EvidenceRepository):
        async def record_web_source(self, scope, reservation_id, draft):
            if draft.text:
                raise ApplicationError("web_storage_limit", "Evidence storage is full.")
            return await super().record_web_source(scope, reservation_id, draft)

    async def exercise():
        repository, _, registry = tools(FullEvidence(), reader=reader())
        result = await registry.execute(
            ModelToolCall("call", "web_read", {"urls": ["https://example.com"]}),
            None,
            context=context(),
        )
        page = json.loads(result.content)["pages"][0]
        assert page["status"] == "budget_exhausted"
        assert page["error_code"] == "web_storage_limit"
        assert not repository.sources[page["source_id"]][1].text
        assert repository.usage.response_bytes == 1000
        assert repository.usage.extracted_bytes == 0

    asyncio.run(exercise())


def test_cancellation_retains_charges_and_fences_unrecorded_worker_results():
    async def exercise():
        active = True

        async def assert_active():
            if not active:
                raise ApplicationError("web_scope_invalid", "Run cancelled.")

        async def cancelled_result(request, trusted_context):
            nonlocal active
            active = False
            return ExtractedDocumentResult(
                status=SourceStatus.SUCCESS,
                requested_url=request.url,
                final_url=request.url,
                media_type="text/plain",
                text="Late result.",
                usage=WebUsage(
                    page_attempts=1, network_requests=1, response_bytes=10, extracted_bytes=12
                ),
            )

        repository, _, registry = tools(
            reader=Mock(read_document=AsyncMock(side_effect=cancelled_result))
        )
        with pytest.raises(ApplicationError, match="Run cancelled"):
            await registry.execute(
                ModelToolCall("call", "web_read", {"urls": ["https://example.com"]}),
                None,
                context=context(_assert_run_active=assert_active),
            )
        assert not repository.sources
        assert repository.usage.page_attempts == 1
        assert repository.usage.response_bytes == 18 * 1024 * 1024
        assert all(item.status == "reserved" for item in repository.reservations.values())

        async def cancelled_search(*args):
            raise asyncio.CancelledError

        repository, _, registry = tools(search=Mock(search=AsyncMock(side_effect=cancelled_search)))
        with pytest.raises(asyncio.CancelledError):
            await registry.execute(
                ModelToolCall("call", "web_search", {"query": "fixture"}), None, context=context()
            )
        assert repository.usage.search_calls == 1
        assert "meter" in repository.operations

    asyncio.run(exercise())


def test_read_readiness_does_not_require_search_key_and_invalid_worker_config_fails_closed(
    monkeypatch, tmp_path
):
    async def exercise():
        settings = Settings(
            _env_file=None,
            enabled_capabilities={"web_read"},
            web_worker_image="sha256:" + "a" * 64,
            web_gateway_image="sha256:" + "b" * 64,
            web_gateway_secret_file=tmp_path / "gateway-secret",
            web_gateway_admin_secret="g" * 32,
        )
        candidate = Mock(
            is_ready=AsyncMock(return_value=True), is_render_ready=AsyncMock(return_value=False)
        )
        constructor = Mock(return_value=candidate)
        monkeypatch.setattr("app.infrastructure.accounts.DockerPublicRetriever", constructor)
        registry = AccountRegistry(settings, {}, {})
        repository = Mock(web_tools_ready=AsyncMock(return_value=True))
        evidence = Mock(repository=repository)
        registrations = await registry._web_tool_registrations(
            repository, evidence, RuntimeEventHub()
        )
        assert {tool.name for tool in registrations} == {"web_source", "web_read"}
        assert all(not tool.requires_workspace for tool in registrations)
        candidate.is_ready.assert_awaited_once()
        reading = next(tool for tool in registrations if tool.name == "web_read")
        assert "render" not in reading.arguments_model.model_json_schema()["properties"]
        candidate.is_render_ready.return_value = True
        registrations = await registry._web_tool_registrations(
            repository, evidence, RuntimeEventHub()
        )
        reading = next(tool for tool in registrations if tool.name == "web_read")
        assert "render" in reading.arguments_model.model_json_schema()["properties"]
        assert all(
            call.kwargs["concurrency"] is registry._web_concurrency
            for call in constructor.call_args_list
        )
        candidate.is_ready.return_value = False
        registrations = await registry._web_tool_registrations(
            repository, evidence, RuntimeEventHub()
        )
        assert {tool.name for tool in registrations} == {"web_source"}
        registry._settings = settings.model_copy(update={"web_worker_image": "unpinned-tag"})
        registrations = await registry._web_tool_registrations(
            repository, evidence, RuntimeEventHub()
        )
        assert {tool.name for tool in registrations} == {"web_source"}

    asyncio.run(exercise())
