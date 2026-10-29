import asyncio
import hashlib
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import httpx
import pytest
from web_fixtures import (
    OTHER,
    OWNER,
    RESERVATION,
    SESSION,
    SOURCE,
    repository,
    reservation_row,
    scope,
    source_draft,
    source_row,
)

from app.application.errors import ApplicationError
from app.application.web_evidence import WebEvidenceService
from app.domain.models import Session
from app.domain.web import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_LINES,
    CitationReference,
    SourceStatus,
    WebSource,
    WebUsage,
    canonical_source_text,
)


def test_canonical_lines_preserve_empty_lines_and_bound_long_unicode_lines():
    text = canonical_source_text("first\r\n\rsecond\0\n" + "🪴" * 2050)
    lines = text.split("\n")
    assert lines[:3] == ["first", "", "second"]
    assert [len(line) for line in lines[3:]] == [2048, 2]
    assert canonical_source_text(text) == text
    draft = source_draft(text=text)
    assert draft.content_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "un-normalized\r\ntext"},
        {"text": "a" * (MAX_SOURCE_BYTES + 1)},
        {"text": ""},
        {"final_url": None},
        {"status": SourceStatus.BLOCKED},
        {"status": SourceStatus.CLEARED},
        {"fetched_at": "2026-10-30T00:00:00"},
    ],
)
def test_source_drafts_reject_inconsistent_or_unbounded_evidence(changes):
    with pytest.raises(ValueError):
        source_draft(**changes)


@pytest.mark.parametrize("usage", [{"response_bytes": -1}, {"search_calls": True}])
def test_web_usage_rejects_negative_and_boolean_counters(usage):
    with pytest.raises(ValueError):
        WebUsage(**usage)


def test_removed_source_has_no_retained_identity_metadata_or_content():
    row = source_row(
        status="cleared",
        line_count=0,
        byte_count=0,
        **dict.fromkeys(
            (
                "requested_url",
                "final_url",
                "canonical_url",
                "title",
                "publisher",
                "published_at",
                "fetched_at",
                "extractor_version",
                "content_hash",
                "error_code",
            )
        ),
    )
    source = WebSource.from_dict(row)
    assert source.status is SourceStatus.CLEARED and not source.status.citable
    with pytest.raises(ValueError):
        WebSource.from_dict({**row, "requested_url": "https://private-query.example"})


def test_historical_evidence_requires_owned_conversation_before_repository_access():
    async def exercise():
        sources, sessions = AsyncMock(), AsyncMock()
        service = WebEvidenceService(sources, sessions, owner_id=OWNER)
        sessions.get_session.return_value = None
        with pytest.raises(ApplicationError) as missing:
            await service.list_sources(SESSION, 0, 20)
        assert missing.value.code == "session_not_found"
        sessions.get_session.return_value = Session(SESSION, OTHER, "Other", "now", "now", 0)
        with pytest.raises(ApplicationError) as other:
            await service.clear_sources(SESSION)
        assert other.value.code == missing.value.code
        sources.list_web_sources.assert_not_awaited()
        sources.clear_web_sources.assert_not_awaited()

    asyncio.run(exercise())


def test_missing_source_and_busy_clear_are_reported_accurately():
    async def exercise():
        sources, sessions = AsyncMock(), AsyncMock()
        sessions.get_session.return_value = Session(SESSION, OWNER, "Chat", "now", "now", 0)
        sources.read_web_source.return_value = None
        sources.clear_web_sources.side_effect = ApplicationError("web_sources_busy", "Active run")
        service = WebEvidenceService(sources, sessions, owner_id=OWNER)
        with pytest.raises(ApplicationError) as absent:
            await service.read_source(SESSION, SOURCE, 1, 20)
        assert absent.value.code == "source_not_found"
        with pytest.raises(ApplicationError) as busy:
            await service.clear_sources(SESSION)
        assert busy.value.code == "web_sources_busy"

    asyncio.run(exercise())


def test_reservation_retry_uses_same_verified_owner_dispatch_and_immutable_allowance():
    requests = []
    usage = WebUsage(page_attempts=1, extracted_bytes=100)

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=reservation_row(usage))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await repository(client).reserve_web_budget(
                scope(), RESERVATION, "page:1", usage
            )
            assert result.reserved == usage and result.actual is None

    asyncio.run(exercise())
    assert len(requests) == 2 and requests[0].content == requests[1].content
    assert requests[0].url.path == "/rest/v1/rpc/web_mutate"
    assert requests[0].headers["Authorization"] == "Bearer verified-user-token"
    payload = json.loads(requests[0].content)
    assert payload == {
        "p_action": "reserve",
        "p_capability": "private-run-capability",
        "p_payload": {
            **scope().to_dict(),
            "reservation_id": RESERVATION,
            "operation_key": "page:1",
            "usage": usage.to_dict(),
        },
    }


def test_claim_does_not_retry_after_an_uncertain_database_response():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ApplicationError) as error:
                await repository(client).claim_web_page(scope(), OTHER)
            assert error.value.code == "database_unavailable"

    asyncio.run(exercise())
    assert len(requests) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": OTHER},
        {"session_id": OTHER},
        {"content_hash": "f" * 64},
        {"byte_count": 1},
        {"requested_url": "https://another.example/article"},
        {"title": "Another source"},
    ],
)
def test_record_source_rejects_wrong_owner_scope_or_changed_content(changes):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=source_row(**changes))
            )
        ) as client:
            with pytest.raises(RuntimeError):
                await repository(client).record_web_source(scope(), RESERVATION, source_draft())

    asyncio.run(exercise())


def test_source_lines_reject_unbounded_response_and_use_no_run_capability():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "source": source_row(),
                "start_line": 1,
                "lines": ["x" * 21_000],
                "total_lines": 2,
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(RuntimeError):
                await repository(client).read_web_source(SESSION, SOURCE)

    asyncio.run(exercise())
    assert json.loads(requests[0].content)["p_capability"] is None


@pytest.mark.parametrize(
    "message,code",
    [
        ("web_sources_busy", "PT409"),
        ("web_scope_invalid", "42501"),
        ("web_budget_exhausted", "22023"),
    ],
)
def test_web_sql_errors_map_to_safe_domain_errors(message, code):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    409 if code == "PT409" else 403 if code == "42501" else 400,
                    json={"code": code, "message": message, "details": "private database detail"},
                )
            )
        ) as client:
            with pytest.raises(ApplicationError) as error:
                await repository(client).clear_web_sources(SESSION)
            assert error.value.code == message
            assert "private database" not in str(error.value)

    asyncio.run(exercise())


def test_citation_ranges_are_bounded_and_require_ordered_positive_lines():
    assert asdict(CitationReference(SOURCE, 1, 2))["end_line"] == 2
    for first, last in ((0, 2), (3, 2), (1, 201)):
        with pytest.raises(ValueError):
            CitationReference(SOURCE, first, last)


def test_cleared_crawl_is_unavailable_instead_of_an_invalid_scope_tombstone():
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=None))
        ) as client:
            assert await repository(client).get_web_crawl(SESSION, OTHER) is None
            pages = await repository(client).list_web_pages(SESSION, OTHER)
            assert pages.items == () and pages.next_offset is None

    asyncio.run(exercise())


def test_maximum_sized_newline_text_preserves_its_last_recorded_line():
    draft = source_draft(text="\n" * MAX_SOURCE_BYTES)
    source = WebSource.from_dict(
        source_row(
            byte_count=MAX_SOURCE_BYTES,
            line_count=MAX_SOURCE_LINES,
            content_hash=draft.content_hash,
        )
    )
    assert source.line_count == MAX_SOURCE_BYTES + 1
    assert (
        CitationReference(SOURCE, MAX_SOURCE_LINES, MAX_SOURCE_LINES).end_line == source.line_count
    )
