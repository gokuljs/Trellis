import asyncio
import json

import httpx
import pytest
from web_fixtures import RESERVATION, SOURCE, repository, scope, source_draft

from app.domain.web import WebUsage

CRAWL = "30000000-0000-4000-8000-000000000009"
URL = "https://example.com/start"


def page_row(**changes):
    return {
        "crawl_id": CRAWL,
        "url": URL,
        "depth": 0,
        "status": "completed",
        "attempt": 1,
        "generation": 2,
        "source_id": SOURCE,
        "discovered_links": [URL + "/child"],
        **changes,
    }


def test_atomic_finalization_retries_only_identical_bounded_payload():
    async def exercise():
        payloads = []
        usage = WebUsage(
            page_attempts=1, network_requests=2, response_bytes=100, extracted_bytes=45
        )

        def handler(request):
            payloads.append(json.loads(request.content))
            return (
                httpx.Response(503, json={})
                if len(payloads) == 1
                else httpx.Response(200, json=page_row())
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await repository(client).finalize_crawl_page(
                scope(), CRAWL, URL, 1, RESERVATION, source_draft(), usage, (URL + "/child",)
            )
        assert result.status == "completed" and result.discovered_links == (URL + "/child",)
        assert payloads[0] == payloads[1]
        assert payloads[0]["p_action"] == "finalize_crawl_page"
        assert payloads[0]["p_payload"]["discovered_links"] == [URL + "/child"]
        assert payloads[0]["p_capability"] == "private-run-capability"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "changes",
    [
        {"url": URL + "/other"},
        {"attempt": 2},
        {"generation": 3},
        {"source_id": RESERVATION},
        {"discovered_links": []},
        {"discovered_links": ["x"] * 33},
    ],
)
def test_atomic_finalization_rejects_changed_page_identity_or_snapshot(changes):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=page_row(**changes))
            )
        ) as client:
            with pytest.raises(RuntimeError):
                await repository(client).finalize_crawl_page(
                    scope(),
                    CRAWL,
                    URL,
                    1,
                    RESERVATION,
                    source_draft(),
                    WebUsage(page_attempts=1),
                    (URL + "/child",),
                )

    asyncio.run(exercise())


@pytest.mark.parametrize("wait", [True, -1, 1.01, "0", None])
def test_origin_pacing_rejects_unbounded_or_ambiguous_retry_after(wait):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"wait_seconds": wait})
            )
        ) as client:
            with pytest.raises(RuntimeError):
                await repository(client).reserve_crawl_origin(scope(), CRAWL, URL, 1)

    asyncio.run(exercise())
