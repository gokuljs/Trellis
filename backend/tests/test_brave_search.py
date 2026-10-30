import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.application.budgets import RunLimits
from app.application.tool_context import ToolExecutionContext
from app.domain.search import MAX_SEARCH_RESPONSE_BYTES, SearchHit, SearchResult, SearchStatus
from app.infrastructure.brave_search import BRAVE_SEARCH_ENDPOINT, BraveSearchProvider


class Stream(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def context():
    return ToolExecutionContext(
        "owner",
        "session",
        "run",
        "tool",
        "dispatch",
        1,
        0,
        datetime.now(UTC) + timedelta(seconds=30),
        RunLimits(8, 16, 100000, 600, 2.0),
        None,
        AsyncMock(),
    )


def response(payload=None, *, status=200, headers=None):
    body = json.dumps(payload if payload is not None else {"web": {"results": []}}).encode()
    return httpx.Response(status, headers=headers, stream=Stream(body))


def test_search_uses_only_fixed_provider_credentials_and_bounded_query():
    async def exercise():
        requests = []

        def handler(request):
            requests.append(request)
            return response(
                {
                    "web": {
                        "results": [
                            {
                                "title": "A public page",
                                "url": "https://example.com/article",
                                "description": "Discovery only.",
                            },
                            {
                                "title": "Other",
                                "url": "https://example.com/other",
                                "description": "Another snippet.",
                            },
                        ]
                    }
                },
                headers={"Set-Cookie": "provider=secret"},
            )

        provider = BraveSearchProvider(
            "operator-fixture-key", transport=httpx.MockTransport(handler)
        )
        assert provider.configured
        assert provider.key_hash == hashlib.sha256(b"operator-fixture-key").hexdigest()
        trusted = context()
        result = await provider.search("public comparison", 1, trusted)
        assert result.status is SearchStatus.SUCCESS
        assert result.hits == (
            SearchHit("A public page", "https://example.com/article", "Discovery only."),
        )
        assert 0 < result.response_bytes < MAX_SEARCH_RESPONSE_BYTES
        await provider.search("another query", 5, context())
        assert len(requests) == 2
        assert trusted._assert_run_active.await_count == 2
        for request in requests:
            assert str(request.url) == BRAVE_SEARCH_ENDPOINT
            assert request.method == "POST"
            assert request.headers["X-Subscription-Token"] == "operator-fixture-key"
            assert request.headers["Accept-Encoding"] == "identity"
            assert "Authorization" not in request.headers and "Cookie" not in request.headers
            assert json.loads(request.content)["result_filter"] == ["web"]
            assert json.loads(request.content)["text_decorations"] is False
        assert json.loads(requests[0].content)["count"] == 1
        assert json.loads(requests[0].content)["q"] == "public comparison"

    asyncio.run(exercise())


def test_missing_key_does_not_contact_provider():
    async def exercise():
        provider = BraveSearchProvider(
            None, transport=httpx.MockTransport(lambda request: pytest.fail("contacted"))
        )
        assert not provider.configured and provider.key_hash is None
        result = await provider.search("public query", 5, context())
        assert result.status is SearchStatus.UNAVAILABLE
        assert result.error_code == "brave_key_missing"

    asyncio.run(exercise())


def test_nullable_snippet_preserves_the_hit_and_other_results():
    async def exercise():
        payload = {
            "web": {
                "results": [
                    {"url": "https://example.com", "title": "Example", "description": None},
                    {
                        "url": "https://example.com/other",
                        "title": "Other",
                        "description": "Available snippet.",
                    },
                ]
            }
        }
        result = await BraveSearchProvider(
            "fixture", transport=httpx.MockTransport(lambda request: response(payload))
        ).search("query", 5, context())

        assert result.status is SearchStatus.SUCCESS
        assert result.hits == (
            SearchHit("Example", "https://example.com", ""),
            SearchHit("Other", "https://example.com/other", "Available snippet."),
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, SearchStatus.INVALID_KEY),
        (403, SearchStatus.INVALID_KEY),
        (429, SearchStatus.RATE_LIMITED),
        (302, SearchStatus.PROVIDER_ERROR),
        (422, SearchStatus.PROVIDER_ERROR),
        (503, SearchStatus.PROVIDER_ERROR),
    ],
)
def test_provider_failures_are_accurate_and_do_not_follow_redirects(status, expected, caplog):
    async def exercise():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return response(
                {"error": "private-provider-body"},
                status=status,
                headers={"Location": "https://example.com/redirect"},
            )

        result = await BraveSearchProvider(
            "fixture-key", transport=httpx.MockTransport(handler)
        ).search("sensitive-query", 5, context())
        assert result.status is expected and not result.hits
        assert calls == 1
        assert "private-provider-body" not in repr(result)

    asyncio.run(exercise())
    assert "sensitive-query" not in caplog.text and "fixture-key" not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "unexpected success status"},
        {"web": "invalid"},
        {"web": {"results": "invalid"}},
        {"web": {"results": [None]}},
        {"web": {"results": [{"url": 123}]}},
    ],
)
def test_invalid_provider_shapes_are_bounded_failures(payload):
    async def exercise():
        result = await BraveSearchProvider(
            "fixture", transport=httpx.MockTransport(lambda request: response(payload))
        ).search("query", 5, context())
        assert result.status is SearchStatus.PROVIDER_ERROR

    asyncio.run(exercise())


def test_search_omits_unusable_urls_without_truncating_valid_urls():
    async def exercise():
        long_url = "https://example.com/" + "a" * 4000
        items = [
            {"url": "https://example.com/" + "x" * 5000},
            {"url": "javascript:alert(1)"},
            {"url": "https://user:secret@example.com/"},
            {"url": long_url, "title": "x" * 2000, "description": "y" * 4000},
        ]
        result = await BraveSearchProvider(
            "fixture",
            transport=httpx.MockTransport(lambda request: response({"web": {"results": items}})),
        ).search("query", 5, context())
        assert result.status is SearchStatus.SUCCESS and len(result.hits) == 1
        assert result.hits[0].url == long_url
        assert len(result.hits[0].title) == 1000 and len(result.hits[0].snippet) == 2048

    asyncio.run(exercise())


def test_large_unicode_results_stay_under_tool_result_allowance():
    async def exercise():
        payload = {
            "web": {
                "results": [
                    {
                        "url": "https://example.com/" + str(index),
                        "title": "🪴" * 1000,
                        "description": "🪴" * 2048,
                    }
                    for index in range(10)
                ]
            }
        }
        result = await BraveSearchProvider(
            "fixture", transport=httpx.MockTransport(lambda request: response(payload))
        ).search("query", 10, context())
        assert result.hits
        assert (
            len(
                json.dumps(
                    [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in result.hits],
                    ensure_ascii=False,
                ).encode()
            )
            < 18000
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "headers,chunks,expected",
    [
        ({"Content-Length": str(MAX_SEARCH_RESPONSE_BYTES + 1)}, [b"{}"], SearchStatus.OVERSIZED),
        ({"Content-Encoding": "gzip"}, [b"compressed"], SearchStatus.PROVIDER_ERROR),
        ({}, [b"x" * MAX_SEARCH_RESPONSE_BYTES, b"x"], SearchStatus.OVERSIZED),
        ({}, [b"NaN"], SearchStatus.PROVIDER_ERROR),
        ({}, [b"not json"], SearchStatus.PROVIDER_ERROR),
    ],
)
def test_encoded_oversized_and_invalid_json_responses_fail_closed(headers, chunks, expected):
    async def exercise():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, stream=Stream(*chunks))
        )
        result = await BraveSearchProvider("fixture", transport=transport).search(
            "query", 5, context()
        )
        assert result.status is expected and result.response_bytes <= MAX_SEARCH_RESPONSE_BYTES

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "query,count",
    [
        (" ", 5),
        ("x" * 513, 5),
        ("word " * 76, 5),
        ("query\nconversation", 5),
        ("query", 11),
        ("query", True),
    ],
)
def test_invalid_queries_cannot_send_conversation_dumps(query, count):
    async def exercise():
        provider = BraveSearchProvider(
            "fixture", transport=httpx.MockTransport(lambda request: pytest.fail("contacted"))
        )
        assert (
            await provider.search(query, count, context())
        ).error_code == "invalid_search_arguments"

    asyncio.run(exercise())


def test_timeout_and_cancellation_do_not_retry():
    async def exercise():
        async def timeout(request):
            raise httpx.ReadTimeout("provider timeout")

        result = await BraveSearchProvider(
            "fixture", transport=httpx.MockTransport(timeout)
        ).search("query", 5, context())
        assert result.status is SearchStatus.TIMEOUT
        entered = asyncio.Event()

        async def waiting(request):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            BraveSearchProvider("fixture", transport=httpx.MockTransport(waiting)).search(
                "query", 5, context()
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


@pytest.mark.parametrize("key", ["key\nother", "a" * 4097, "non-ascii-🪴"])
def test_operator_credentials_cannot_inject_headers(key):
    with pytest.raises(ValueError):
        BraveSearchProvider(key)


def test_search_domain_rejects_failed_hits_or_unbounded_fields():
    with pytest.raises(ValueError):
        SearchResult(SearchStatus.PROVIDER_ERROR, (SearchHit("title", "https://example.com", ""),))
    with pytest.raises(ValueError):
        SearchHit("title", "", "")


def test_empty_search_response_is_successful_without_claiming_a_page_was_read():
    async def exercise():
        transport = httpx.MockTransport(lambda request: response({"type": "search", "web": None}))
        result = await BraveSearchProvider("fixture", transport=transport).search(
            "query", 5, context()
        )
        assert result.status is SearchStatus.SUCCESS and result.hits == ()

    asyncio.run(exercise())
