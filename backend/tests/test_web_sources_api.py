import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from web_fixtures import SESSION, SOURCE, source_row

from app.api.dependencies import get_web_evidence_service
from app.api.routes.sources import router
from app.application.errors import ApplicationError
from app.domain.web import CitationBinding, ClearSourcesResult, SourceLines, SourcePage, WebSource
from app.main import ERROR_STATUS


def test_source_http_contract_is_bounded_and_omits_private_runtime_identity():
    source = WebSource.from_dict(source_row())
    service = AsyncMock()
    service.list_sources.return_value = SourcePage((source,), None)
    service.read_source.return_value = SourceLines(source, 1, ("First supporting line.",), 2)
    service.clear_sources.return_value = ClearSourcesResult(1, 1)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_web_evidence_service] = lambda: service

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            listing = await client.get(f"/api/sessions/{SESSION}/sources")
            assert listing.status_code == 200
            item = listing.json()["items"][0]
            assert item["id"] == SOURCE and item["status"] == "success"
            assert "user_id" not in item and "text" not in item and "evidence_epoch" not in item
            lines = await client.get(f"/api/sessions/{SESSION}/sources/{SOURCE}?max_lines=1")
            assert lines.json()["lines"] == ["First supporting line."]
            invalid = await client.get(f"/api/sessions/{SESSION}/sources/{SOURCE}?max_lines=201")
            assert invalid.status_code == 422
            cleared = await client.delete(f"/api/sessions/{SESSION}/sources")
            assert cleared.json() == {"evidence_epoch": 1, "cleared_count": 1}

    asyncio.run(exercise())
    service.read_source.assert_awaited_once_with(SESSION, SOURCE, 1, 1)


def test_citation_http_contract_exposes_only_owned_answer_bindings():
    call_id = "32000000-0000-4000-8000-000000000001"
    service = AsyncMock()
    service.read_citations.return_value = (CitationBinding(call_id, SOURCE, 1, 2, 1),)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_web_evidence_service] = lambda: service

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/api/sessions/{SESSION}/citations/{call_id}")
            assert response.status_code == 200
            assert response.json() == {
                "items": [
                    {
                        "model_call_id": call_id,
                        "source_id": SOURCE,
                        "start_line": 1,
                        "end_line": 2,
                        "citation_index": 1,
                    }
                ]
            }
            invalid = await client.get(f"/api/sessions/{SESSION}/citations/not-a-uuid")
            assert invalid.status_code == 422

    asyncio.run(exercise())
    service.read_citations.assert_awaited_once_with(SESSION, call_id)


@pytest.mark.parametrize(
    "error_code,status", [("source_not_found", 404), ("web_sources_busy", 409)]
)
def test_source_http_errors_preserve_not_found_and_busy_outcomes(error_code, status):
    app = FastAPI()
    app.include_router(router)
    service = AsyncMock()
    service.read_source.side_effect = ApplicationError(error_code, "Safe failure")
    app.dependency_overrides[get_web_evidence_service] = lambda: service

    @app.exception_handler(ApplicationError)
    async def handle(_request, error):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}},
            status_code=ERROR_STATUS[error.code],
        )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get(f"/api/sessions/{SESSION}/sources/{SOURCE}")
            assert result.status_code == status
            assert result.json()["error"]["code"] == error_code

    asyncio.run(exercise())
