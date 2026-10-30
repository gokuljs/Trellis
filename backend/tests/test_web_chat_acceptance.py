import asyncio

import pytest
from web_chat_acceptance import (
    CHAT_TEXTS,
    CHAT_URLS,
    run_bounded_crawl_chat,
    run_web_chat,
    verify_normal_web_chat,
)

from app.domain.retrieval import ExtractedDocumentResult
from app.domain.runtime import RunStatus
from app.domain.web import SourceStatus, WebUsage


class FixtureRetriever:
    def __init__(self):
        self.requests = []

    async def is_ready(self):
        return True

    async def read_document(self, request, context):
        await context.assert_active()
        self.requests.append(request)
        text = CHAT_TEXTS[CHAT_URLS.index(request.url)]
        return ExtractedDocumentResult(
            status=SourceStatus.SUCCESS,
            requested_url=request.url,
            final_url=request.url,
            media_type="text/plain",
            text=text,
            usage=WebUsage(
                page_attempts=1,
                network_requests=1,
                response_bytes=512,
                extracted_bytes=len(text.encode()),
            ),
        )


def test_normal_web_chat_searches_retrieves_both_pages_reads_lines_and_finalizes_citations():
    async def exercise():
        retriever = FixtureRetriever()
        proof = await verify_normal_web_chat(retriever)
        assert [request.url for request in retriever.requests] == list(CHAT_URLS)
        assert proof == {
            "model_calls": 4,
            "tool_calls": 4,
            "retrieved_pages": 2,
            "citation_bindings": 2,
            "workspace_accesses": 0,
            "persistence": "in_memory_contract_fixture",
        }

    asyncio.run(exercise())


def test_normal_chat_crawl_stops_at_two_pages_reads_sources_and_finalizes_citations():
    async def exercise():
        repository, provider = await run_bounded_crawl_chat()
        assert repository.run.status is RunStatus.COMPLETED
        assert repository.final_content is not None
        assert "stopped at its two-page limit" in repository.final_content
        assert tuple(binding.source_id for binding in repository.bindings) == provider.source_ids
        assert len(repository.bindings) == 2
        assert all(binding.start_line == binding.end_line == 1 for binding in repository.bindings)
        assert repository.lifecycle.index("bound") < repository.lifecycle.index("promoted")

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["workspace_read", "workspace_patch", "workspace_command"])
def test_source_injection_cannot_grant_workspace_capabilities(mode):
    async def exercise():
        repository, provider = await run_web_chat(FixtureRetriever(), mode=mode)
        assert repository.run.status is RunStatus.FAILED
        assert repository.events[-1].data["code"] == "capability_disabled"
        assert provider.requests[-1].messages[-1].role == "tool"
        assert repository.final_content is None and not repository.bindings
        assert {tool.name for tool in repository.tool_calls} == {
            "web_search",
            "web_read",
            "web_source",
        }

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["unknown_citation", "invalid_range"])
def test_final_answer_cannot_promote_citations_without_available_supporting_lines(mode):
    async def exercise():
        repository, _ = await run_web_chat(FixtureRetriever(), mode=mode)
        assert repository.run.status is RunStatus.FAILED
        assert repository.events[-1].data["code"] == "citation_invalid"
        assert repository.final_content is None and not repository.bindings
        assert "promoted" not in repository.lifecycle

    asyncio.run(exercise())
