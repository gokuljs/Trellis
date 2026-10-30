import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_retrieval_worker import DEPLOYMENT, context, document_payload, inspection

from app.domain.retrieval import RetrievalRequest
from app.domain.web import SourceStatus, WebUsage
from app.infrastructure import retrieval_worker as worker


@pytest.mark.parametrize(
    "labels,ready",
    [
        ({"trellis.worker.protocol": "public-document-v1"}, False),
        ({"trellis.crawl.protocol": "public-crawl-v1"}, True),
        ({"trellis.crawl.protocol": "public-crawl-v0"}, False),
        (None, False),
    ],
)
def test_crawl_readiness_requires_its_distinct_image_protocol(monkeypatch, labels, ready):
    async def exercise():
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, AsyncMock())
        adapter.is_ready = AsyncMock(return_value=True)
        monkeypatch.setattr(
            worker,
            "_run_docker",
            AsyncMock(return_value=(0, json.dumps([{"Config": {"Labels": labels}}]).encode())),
        )
        assert await adapter.is_crawl_ready() is ready
        adapter.is_ready.return_value = False
        assert await adapter.is_crawl_ready() is False

    asyncio.run(exercise())


def test_old_reading_image_never_receives_a_crawl_lease(monkeypatch):
    async def exercise():
        control = AsyncMock()
        control.revoke.return_value = None
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control)
        adapter.is_ready = AsyncMock(return_value=True)
        monkeypatch.setattr(
            worker,
            "_run_docker",
            AsyncMock(
                return_value=(
                    0,
                    b'[{"Config":{"Labels":{"trellis.worker.protocol":"public-document-v1"}}}]',
                )
            ),
        )
        result = await adapter.crawl_document(
            RetrievalRequest("https://example.com/", scope_origin="https://example.com"), context()
        )
        assert result.status is SourceStatus.UNAVAILABLE
        assert result.error_code == "worker_unavailable"
        control.issue.assert_not_awaited()

    asyncio.run(exercise())


def test_crawl_worker_operation_is_scoped_and_preserves_gateway_receipts(monkeypatch):
    async def exercise():
        request = RetrievalRequest(
            "https://example.com/", scope_origin="https://example.com", max_requests=7
        )
        control = AsyncMock()
        control.issue.return_value = "lease-token"
        control.revoke.return_value = worker.GatewayReceipt(2, 6000)
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control)
        adapter.is_crawl_ready = AsyncMock(return_value=True)
        adapter._inspect = AsyncMock(return_value=inspection())

        async def docker(args, **kwargs):
            if args[0] == "start":
                payload = json.loads(kwargs["payload"])
                assert payload["operation"] == "crawl"
                assert payload["scope_origin"] == "https://example.com"
                assert payload["max_requests"] == 7
                return 0, document_payload(request)
            return 0, b""

        monkeypatch.setattr(worker, "_run_docker", docker)
        result = await adapter.crawl_document(request, context())
        assert result.status is SourceStatus.SUCCESS
        assert result.usage == WebUsage(
            page_attempts=1, network_requests=2, response_bytes=6000, extracted_bytes=16
        )
        with pytest.raises(ValueError, match="bounded scope"):
            await adapter.crawl_document(RetrievalRequest("https://example.com/"), context())

    asyncio.run(exercise())
