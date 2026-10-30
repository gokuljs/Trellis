import asyncio
import json
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from test_web_tools import EvidenceRepository, context
from web_fixtures import OWNER, RUN, SESSION, SOURCE, TOOL, source_draft

from app.application.errors import ApplicationError
from app.application.secret_redaction import SecretRedactor
from app.application.tools import MAX_RESULT_BYTES, ToolRegistry
from app.application.web_crawl import WebCrawlArguments, WebCrawlService
from app.application.web_evidence import WebEvidenceService
from app.application.web_ports import WebRepository
from app.application.web_tools import WebToolService
from app.core.config import Settings
from app.domain.capabilities import RuntimeCapabilities
from app.domain.crawl import CrawlScope
from app.domain.models import Session
from app.domain.retrieval import ExtractedDocumentResult
from app.domain.runtime import ModelToolCall
from app.domain.web import CrawlPage, CrawlPageBatch, SourceStatus, WebCrawl, WebScope, WebUsage
from app.infrastructure.accounts import AccountRegistry
from app.infrastructure.runtime_events import RuntimeEventHub


class CrawlRepository(EvidenceRepository):
    def __init__(self):
        super().__init__()
        self.crawl = None
        self.pages = {}
        self.finalized = []
        self.waits = []
        self.crash_after_finalize = False
        self.full_storage = False
        self.stop_at_frontier = True

    async def get_web_crawl(self, session_id, crawl_id):
        return self.crawl

    async def create_web_crawl(self, scope, crawl_id, seed, origin, prefix, max_pages, max_depth):
        if self.crawl is None:
            self.crawl = WebCrawl(
                crawl_id,
                OWNER,
                SESSION,
                RUN,
                TOOL,
                0,
                seed,
                origin,
                prefix,
                max_pages,
                max_depth,
                "running",
                None,
                "now",
                "now",
            )
        return self.crawl

    async def enqueue_web_pages(self, scope, crawl_id, pages):
        added = 0
        for url, depth in pages:
            if url not in self.pages and len(self.pages) < 200:
                self.pages[url] = CrawlPage(crawl_id, url, depth, "queued", 0, 0, None)
                added += 1
        return added

    async def list_web_pages(self, session_id, crawl_id, offset=0, limit=200):
        return CrawlPageBatch(
            tuple(sorted(self.pages.values(), key=lambda p: (p.depth, p.url))), None
        )

    async def claim_web_page(self, scope, crawl_id):
        assert self.crawl is not None
        for page in (await self.list_web_pages(SESSION, crawl_id)).items:
            if page.status == "completed":
                continue
            if page.status == "fetching" and page.generation >= scope.generation:
                continue
            if (
                page.status == "queued"
                and sum(p.attempt > 0 for p in self.pages.values()) >= self.crawl.max_pages
            ):
                continue
            claimed = replace(
                page, status="fetching", attempt=page.attempt + 1, generation=scope.generation
            )
            self.pages[page.url] = claimed
            return claimed
        return None

    async def reserve_crawl_origin(self, scope, crawl_id, url, page_attempt):
        self.operations.append("pace")
        return self.waits.pop(0) if self.waits else 0

    async def finalize_crawl_page(
        self, scope, crawl_id, url, page_attempt, reservation_id, source, usage, discovered_links
    ):
        if self.full_storage and source.text:
            raise ApplicationError("web_storage_limit", "Storage is full.")
        page = self.pages[url]
        assert page.attempt == page_attempt and page.generation == scope.generation
        await self.record_web_source(scope, reservation_id, source)
        await self.settle_web_budget(scope, reservation_id, usage)
        completed = replace(
            page, status="completed", source_id=source.id, discovered_links=discovered_links
        )
        self.pages[url] = completed
        assert self.crawl is not None
        if page.depth < self.crawl.max_depth:
            overflow = len(self.pages) + len(set(discovered_links) - self.pages.keys()) > 200
            await self.enqueue_web_pages(
                scope, crawl_id, tuple((link, page.depth + 1) for link in discovered_links)
            )
            if overflow and self.stop_at_frontier:
                self.crawl = replace(self.crawl, status="stopped", stop_reason="frontier_limit")
        self.finalized.append(completed)
        if self.crash_after_finalize:
            self.crash_after_finalize = False
            raise asyncio.CancelledError
        return completed

    async def checkpoint_web_crawl(self, scope, crawl_id, status, stop_reason=None):
        assert self.crawl is not None
        self.crawl = replace(self.crawl, status=status, stop_reason=stop_reason)
        return self.crawl


def crawl_service(repository, reader, redactor):
    return WebCrawlService(cast(WebRepository, repository), reader, redactor)


def scope_for(trusted_context):
    return WebScope(
        trusted_context.user_id,
        trusted_context.session_id,
        trusted_context.run_id,
        trusted_context.tool_call_id,
        trusted_context.dispatch_id,
        trusted_context.dispatch_attempt,
        trusted_context.generation,
        0,
    )


def crawler(pages, *, before=None):
    async def fetch(request, trusted_context):
        await trusted_context.assert_active()
        if before is not None:
            await before(request)
        text, links = pages[request.url]
        return ExtractedDocumentResult(
            SourceStatus.SUCCESS,
            request.url,
            final_url=request.url,
            media_type="text/html",
            text=text,
            links=tuple(links),
            canonical_url="https://elsewhere.example/untrusted",
            usage=WebUsage(
                page_attempts=1,
                network_requests=2,
                response_bytes=100,
                extracted_bytes=len(text.encode()),
            ),
        )

    return Mock(crawl_document=AsyncMock(side_effect=fetch))


def test_crawl_follows_bounded_scope_deduplicates_fragments_and_preserves_queries():
    async def exercise():
        repository = CrawlRepository()
        seed = "https://example.com/docs"
        child = "https://example.com/docs/a?q=one"
        reader = crawler(
            {
                seed: (
                    "Seed evidence.",
                    [
                        child + "#first",
                        child + "#second",
                        "https://example.com/docsevil",
                        "https://elsewhere.example/docs",
                        "https://example.com/docs/%252e%252e/private",
                        "https://example.com/docs/secret?token=private-value",
                    ],
                ),
                child: ("Child evidence.", ["https://example.com/docs/grandchild"]),
            }
        )
        service = crawl_service(repository, reader, SecretRedactor())
        trusted = context()
        result = await service.execute(
            WebCrawlArguments(seed_url=seed, path_prefix="/docs", max_depth=1),
            scope_for(trusted),
            trusted,
        )
        assert result["status"] == "completed" and result["stop_reason"] == "frontier_exhausted"
        assert result["completed_pages"] == 2
        assert set(repository.pages) == {seed, child}
        assert repository.pages[seed].discovered_links == (child,)
        assert reader.crawl_document.await_count == 2
        assert repository.usage == WebUsage(
            page_attempts=2, network_requests=4, response_bytes=200, extracted_bytes=29
        )
        outcomes = result["pages"]
        assert isinstance(outcomes, list)
        assert all("text" not in page for page in outcomes)
        assert len(json.dumps(result).encode()) < MAX_RESULT_BYTES

    asyncio.run(exercise())


def test_completed_page_and_link_snapshot_survive_lost_finalize_response_without_refetch():
    async def exercise():
        repository = CrawlRepository()
        seed, child = "https://example.com/docs", "https://example.com/docs/child"
        reader = crawler({seed: ("Seed.", [child]), child: ("Child.", [])})
        service = crawl_service(repository, reader, SecretRedactor())
        repository.crash_after_finalize = True
        trusted = context()
        arguments = WebCrawlArguments(seed_url=seed)
        with pytest.raises(asyncio.CancelledError):
            await service.execute(arguments, scope_for(trusted), trusted)
        assert repository.pages[seed].status == "completed" and child in repository.pages
        recovered = replace(trusted, dispatch_id=str(uuid4()), dispatch_attempt=2, generation=3)
        result = await service.execute(arguments, scope_for(recovered), recovered)
        assert result["completed_pages"] == 2
        assert [call.args[0].url for call in reader.crawl_document.await_args_list] == [seed, child]
        assert repository.usage.page_attempts == 2
        again = await service.execute(arguments, scope_for(recovered), recovered)
        assert again == result and reader.crawl_document.await_count == 2

    asyncio.run(exercise())


def test_unfinished_takeover_gets_new_attempt_and_keeps_conservative_prior_charge():
    async def exercise():
        repository = CrawlRepository()
        seed = "https://example.com/"
        reader = Mock(crawl_document=AsyncMock(side_effect=asyncio.CancelledError))
        service = crawl_service(repository, reader, SecretRedactor())
        trusted = context()
        arguments = WebCrawlArguments(seed_url=seed)
        with pytest.raises(asyncio.CancelledError):
            await service.execute(arguments, scope_for(trusted), trusted)
        assert (
            repository.usage.page_attempts == 1
            and repository.usage.response_bytes == 18 * 1024 * 1024
        )
        reader.crawl_document = crawler({seed: ("Recovered.", [])}).crawl_document
        recovered = replace(trusted, dispatch_id=str(uuid4()), dispatch_attempt=2, generation=3)
        result = await service.execute(arguments, scope_for(recovered), recovered)
        assert result["completed_pages"] == 1
        assert result["claimed_attempts"] == 2
        assert result["run_page_attempts_charged"] == 2
        assert repository.pages[seed].attempt == 2
        assert repository.usage.page_attempts == 2
        assert len(repository.reservations) == 2
        assert sum(item.status == "reserved" for item in repository.reservations.values()) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "seed",
    [
        "https://example.com/?token=private-value",
        "https://example.com/?%74oken=private-value",
        "https://example.com/?%2574oken=private-value",
        "http://127.0.0.1/",
        "https://example.com/docs/%252e%252e/private",
    ],
)
def test_sensitive_or_ambiguous_seed_never_enters_frontier_or_worker(seed):
    async def exercise():
        repository = CrawlRepository()
        reader = crawler({})
        trusted = context()
        result = await crawl_service(repository, reader, SecretRedactor()).execute(
            WebCrawlArguments(seed_url=seed), scope_for(trusted), trusted
        )
        assert result["status"] == "blocked"
        assert repository.crawl is None and not repository.reservations
        reader.crawl_document.assert_not_awaited()

    asyncio.run(exercise())


def test_shared_exhausted_run_and_page_limit_stop_without_extra_fetches():
    async def exercise():
        seed = "https://example.com/"
        repository = CrawlRepository()
        repository.usage = WebUsage(page_attempts=20)
        reader = crawler({})
        trusted = context()
        result = await crawl_service(repository, reader, SecretRedactor()).execute(
            WebCrawlArguments(seed_url=seed), scope_for(trusted), trusted
        )
        assert result["stop_reason"] == "budget_exhausted"
        reader.crawl_document.assert_not_awaited()
        repository = CrawlRepository()
        reader = crawler({seed: ("Seed.", [seed + "a", seed + "b"])})
        result = await crawl_service(repository, reader, SecretRedactor()).execute(
            WebCrawlArguments(seed_url=seed, max_pages=1), scope_for(trusted), trusted
        )
        assert result["stop_reason"] == "page_limit" and result["completed_pages"] == 1
        assert reader.crawl_document.await_count == 1 and len(repository.pages) == 3

    asyncio.run(exercise())


def test_storage_failure_is_recorded_and_network_usage_remains_charged():
    async def exercise():
        repository = CrawlRepository()
        repository.full_storage = True
        seed = "https://example.com/"
        trusted = context()
        result = await crawl_service(
            repository, crawler({seed: ("Seed.", [seed + "a"])}), SecretRedactor()
        ).execute(WebCrawlArguments(seed_url=seed), scope_for(trusted), trusted)
        assert result["stop_reason"] == "storage_limit"
        outcomes = result["pages"]
        assert isinstance(outcomes, list)
        assert outcomes[0]["status"] == "budget_exhausted"
        assert repository.usage.response_bytes == 100 and repository.usage.extracted_bytes == 0
        assert len(repository.pages) == 1 and repository.finalized[0].discovered_links == ()

    asyncio.run(exercise())


def test_pacing_wait_is_cancellation_safe_and_checks_deadline(monkeypatch):
    async def exercise():
        repository = CrawlRepository()
        repository.waits = [0.1]
        seed = "https://example.com/"
        reader = crawler({seed: ("Seed.", [])})
        trusted = context()
        sleep = AsyncMock(side_effect=asyncio.CancelledError)
        monkeypatch.setattr("app.application.web_crawl.asyncio.sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await crawl_service(repository, reader, SecretRedactor()).execute(
                WebCrawlArguments(seed_url=seed), scope_for(trusted), trusted
            )
        reader.crawl_document.assert_not_awaited()
        assert repository.usage.page_attempts == 1 and not repository.sources
        repository = CrawlRepository()
        repository.waits = [trusted.remaining_seconds + 1]
        result = await crawl_service(repository, reader, SecretRedactor()).execute(
            WebCrawlArguments(seed_url=seed), scope_for(trusted), trusted
        )
        assert result["status"] == "stopped" and result["stop_reason"] == "deadline"

    asyncio.run(exercise())


def test_crawl_tool_is_available_in_fresh_chat_without_workspace():
    async def exercise():
        repository = CrawlRepository()
        evidence = WebEvidenceService(
            cast(WebRepository, repository),
            Mock(
                get_session=AsyncMock(
                    return_value=Session(SESSION, OWNER, "title", "now", "now", 0)
                )
            ),
            owner_id=OWNER,
        )
        seed = "https://example.com/"
        service = WebToolService(evidence, crawler=crawler({seed: ("Seed.", [])}))
        registrations = service.registrations(
            search_ready=False, read_ready=False, crawl_ready=True, source_capability="web_crawl"
        )
        registry = ToolRegistry(
            capabilities=RuntimeCapabilities(frozenset({"web_crawl"})),
            additional_tools=registrations,
        )
        assert {item.name for item in registrations} == {"web_source", "web_crawl"}
        assert all(not item.requires_workspace for item in registrations)
        result = await registry.execute(
            ModelToolCall("call", "web_crawl", {"seed_url": seed}), None, context=context()
        )
        payload = json.loads(result.content)
        assert payload["completed_pages"] == 1 and not result.is_error
        source_id = payload["pages"][0]["source_id"]
        result = await registry.execute(
            ModelToolCall("call2", "web_source", {"source_id": source_id}), None, context=context()
        )
        assert json.loads(result.content)["untrusted_lines"][0]["text"] == "Seed."

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/docs/../private",
        "https://example.com/docs/%252f..%252fprivate",
        "https://example.com/docs/%25252525252e",
        "https://example.com/docsevil",
        "https://example.com:80/docs",
        "https://other.example/docs",
    ],
)
def test_segment_scope_rejects_origin_and_encoded_traversal_escapes(url):
    _, scope = CrawlScope.from_seed("https://EXAMPLE.com:443/docs#fragment", "/docs/")
    with pytest.raises(ValueError):
        scope.accept(url)


def test_scope_normalization_preserves_query_and_decodes_prefix_consistently():
    seed, scope = CrawlScope.from_seed("https://EXAMPLE.com:443/docs?q=first#fragment", "/%64ocs")
    assert seed == "https://example.com/docs?q=first"
    assert scope.origin == "https://example.com" and scope.path_prefix == "/docs"
    assert (
        scope.accept("https://example.com/%64ocs/a?q=next#fragment")
        == "https://example.com/%64ocs/a?q=next"
    )


def test_encoded_configured_secret_is_filtered_before_snapshot_or_fetch():
    async def exercise():
        repository = CrawlRepository()
        seed = "https://example.com/"
        secret_link = seed + "?x=%66%69%78%74%75%72%65%2d%70%72%69%76%61%74%65"
        redactor = SecretRedactor("fixture-private")
        reader = crawler({seed: ("Public fact.", [secret_link])})
        trusted = context()
        result = await crawl_service(repository, reader, redactor).execute(
            WebCrawlArguments(seed_url=seed), scope_for(trusted), trusted
        )
        assert result["completed_pages"] == 1
        assert repository.pages[seed].discovered_links == ()
        reader.crawl_document.assert_awaited_once()
        repository = CrawlRepository()
        result = await crawl_service(repository, reader, redactor).execute(
            WebCrawlArguments(seed_url=secret_link), scope_for(trusted), trusted
        )
        assert result["stop_reason"] == "sensitive_url" and repository.crawl is None

    asyncio.run(exercise())


def test_twenty_page_limit_and_two_depths_bound_a_dense_frontier():
    async def exercise():
        repository = CrawlRepository()
        seed = "https://example.com/"
        pages = {seed: ("Seed.", [seed + str(index) for index in range(32)])}
        pages.update(
            {seed + str(index): ("Child.", [seed + f"{index}/child"]) for index in range(32)}
        )
        reader = crawler(pages)
        trusted = context()
        result = await crawl_service(repository, reader, SecretRedactor()).execute(
            WebCrawlArguments(seed_url=seed, max_pages=20, max_depth=2), scope_for(trusted), trusted
        )
        assert result["stop_reason"] == "page_limit" and result["completed_pages"] == 20
        assert reader.crawl_document.await_count == 20
        assert len(repository.pages) == 52
        assert max(page.depth for page in repository.pages.values()) == 2
        assert all(len(page.discovered_links) <= 32 for page in repository.finalized)
        assert repository.usage.page_attempts == 20

    asyncio.run(exercise())


def test_frontier_overflow_stops_with_saved_evidence_and_link_snapshot():
    async def exercise():
        repository = CrawlRepository()
        seed = "https://example.com/"
        pages = {seed: ("Seed.", [seed + str(index) for index in range(32)])}
        pages.update(
            {
                seed + str(index): ("Child.", [seed + f"{index}/{child}" for child in range(32)])
                for index in range(32)
            }
        )
        reader = crawler(pages)
        trusted = context()
        service = crawl_service(repository, reader, SecretRedactor())
        arguments = WebCrawlArguments(seed_url=seed, max_pages=20, max_depth=2)
        result = await service.execute(arguments, scope_for(trusted), trusted)
        assert result["status"] == "stopped" and result["stop_reason"] == "frontier_limit"
        assert result["completed_pages"] == 7 and reader.crawl_document.await_count == 7
        assert len(repository.pages) == 200
        assert len(repository.finalized[-1].discovered_links) == 32
        assert repository.finalized[-1].source_id in repository.sources
        assert all(item.status == "settled" for item in repository.reservations.values())
        assert await service.execute(arguments, scope_for(trusted), trusted) == result
        assert reader.crawl_document.await_count == 7

    asyncio.run(exercise())


def test_pacing_deadline_keeps_previously_completed_source_in_bounded_summary():
    async def exercise():
        repository = CrawlRepository()
        seed, child = "https://example.com/", "https://example.com/child"
        reader = crawler({seed: ("Seed.", [child]), child: ("Child.", [])})
        trusted = context()
        service = crawl_service(repository, reader, SecretRedactor())
        arguments = WebCrawlArguments(seed_url=seed)
        repository.crash_after_finalize = True
        with pytest.raises(asyncio.CancelledError):
            await service.execute(arguments, scope_for(trusted), trusted)
        repository.waits = [trusted.remaining_seconds + 1]
        recovered = replace(trusted, dispatch_id=str(uuid4()), dispatch_attempt=2, generation=3)
        result = await service.execute(arguments, scope_for(recovered), recovered)
        assert result["status"] == "stopped" and result["stop_reason"] == "deadline"
        assert result["completed_pages"] == 1
        assert reader.crawl_document.await_count == 1
        assert repository.usage.page_attempts == 2

    asyncio.run(exercise())


def test_encoded_secret_canonical_metadata_is_omitted_before_source_storage():
    repository = CrawlRepository()
    service = crawl_service(repository, None, SecretRedactor("fixture-private"))
    canonical = "https://example.com/?x=%66%69%78%74%75%72%65%2d%70%72%69%76%61%74%65"
    result = ExtractedDocumentResult(
        SourceStatus.SUCCESS,
        "https://example.com/",
        final_url="https://example.com/",
        canonical_url=canonical,
        media_type="text/plain",
        text="Fact.",
        usage=WebUsage(page_attempts=1, extracted_bytes=5),
    )
    draft = service._draft(result, str(uuid4()), result.requested_url)
    assert draft.canonical_url is None and draft.status is SourceStatus.PARTIAL
    assert draft.error_code == "sensitive_content_redacted"


def test_crawl_only_account_initializes_reader_and_source_tools(monkeypatch, tmp_path):
    async def exercise():
        settings = Settings(
            _env_file=None,
            enabled_capabilities={"web_crawl"},
            web_worker_image="sha256:" + "a" * 64,
            web_gateway_image="sha256:" + "b" * 64,
            web_gateway_secret_file=tmp_path / "gateway-secret",
            web_gateway_admin_secret="g" * 32,
        )
        candidate = Mock(
            is_ready=AsyncMock(return_value=True),
            is_render_ready=AsyncMock(),
            is_crawl_ready=AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "app.infrastructure.accounts.DockerPublicRetriever", Mock(return_value=candidate)
        )
        registry = AccountRegistry(settings, {}, {})
        repository = Mock(web_tools_ready=AsyncMock(return_value=True))
        registrations = await registry._web_tool_registrations(
            repository, Mock(repository=repository), RuntimeEventHub()
        )
        assert {tool.name for tool in registrations} == {"web_source", "web_crawl"}
        candidate.is_ready.assert_awaited_once()
        candidate.is_crawl_ready.assert_awaited_once()
        candidate.is_render_ready.assert_not_awaited()
        assert all(tool.required_capabilities == frozenset({"web_crawl"}) for tool in registrations)
        assert all(not tool.requires_workspace for tool in registrations)
        candidate.is_crawl_ready.return_value = False
        registrations = await registry._web_tool_registrations(
            repository, Mock(repository=repository), RuntimeEventHub()
        )
        assert "web_crawl" not in {tool.name for tool in registrations}
        assert registrations[0].required_capabilities == frozenset({"web_crawl"})

    asyncio.run(exercise())


def test_crawl_only_saved_source_remains_readable_without_worker_configuration():
    async def exercise():
        repository = CrawlRepository()
        trusted = context()
        repository.sources[SOURCE] = (scope_for(trusted), source_draft())
        sessions = Mock(
            get_session=AsyncMock(return_value=Session(SESSION, OWNER, "title", "now", "now", 0))
        )
        evidence = WebEvidenceService(cast(WebRepository, repository), sessions, owner_id=OWNER)
        account = AccountRegistry(
            Settings(_env_file=None, enabled_capabilities={"web_crawl"}), {}, {}
        )
        registrations = await account._web_tool_registrations(
            Mock(web_tools_ready=AsyncMock(return_value=True)), evidence, RuntimeEventHub()
        )
        assert {tool.name for tool in registrations} == {"web_source"}
        registry = ToolRegistry(
            capabilities=RuntimeCapabilities(frozenset({"web_crawl"})),
            additional_tools=registrations,
        )
        result = await registry.execute(
            ModelToolCall("call", "web_source", {"source_id": SOURCE}), None, context=trusted
        )
        assert json.loads(result.content)["untrusted_lines"][0]["text"] == "First supporting line."
        assert not repository.reservations

    asyncio.run(exercise())
