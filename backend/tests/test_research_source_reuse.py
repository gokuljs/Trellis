import asyncio
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from app.application.budgets import limits_for_preset
from app.application.errors import ApplicationError
from app.application.ports import SessionRepository
from app.application.retrieval_ports import BrowserRenderer, DocumentRetriever
from app.application.tool_context import ToolExecutionContext
from app.application.web_evidence import WebEvidenceService
from app.application.web_ports import WebRepository
from app.application.web_tools import WebToolService
from app.domain.research import LEGACY_RESEARCH_POLICY, RESEARCH_POLICY_V2
from app.domain.retrieval import ExtractedDocumentResult
from app.domain.web import SourceLines, SourceStatus, WebRunState, WebSource, WebUsage

OWNER, SESSION, RUN = (str(uuid4()) for _ in range(3))
URL = "https://example.com/paper"


class Repository:
    def __init__(self):
        self.pages: dict[str, tuple[WebSource, tuple[str, ...]]] = {}
        self.reserved: list[object] = []
        self.settled: list[object] = []
        self.listed = 0
        self.after_inventory = None
        self.hydrated_source = None
        self.stale = False
        self.inventory_override = None

    async def open_web_run(self, dispatch):
        if self.stale:
            raise ApplicationError("web_scope_invalid", "Stale generation")
        return WebRunState(0, WebUsage())

    async def list_run_web_sources(self, session, run, limit=40):
        self.listed += 1
        rows = (
            self.inventory_override
            if self.inventory_override is not None
            else tuple(
                page[0]
                for page in self.pages.values()
                if page[0].session_id == session and page[0].run_id == run
            )
        )
        if self.after_inventory:
            self.after_inventory()
        return rows

    async def read_web_source(self, session, source, start=1, max_lines=100):
        found = self.pages.get(source)
        if found is None:
            return None
        metadata, lines = found
        if self.hydrated_source is not None:
            metadata = self.hydrated_source
        return SourceLines(metadata, start, lines[start - 1 : start - 1 + max_lines], len(lines))

    async def reserve_web_budget(self, scope, reservation_id, operation, usage):
        self.reserved.append(usage)

    async def settle_web_budget(self, scope, reservation_id, usage):
        self.settled.append(usage)

    async def record_web_source(self, scope, reservation_id, draft):
        source = WebSource(
            draft.id,
            scope.user_id,
            scope.session_id,
            scope.run_id,
            scope.tool_call_id,
            scope.evidence_epoch,
            draft.requested_url,
            draft.final_url,
            draft.canonical_url,
            draft.title,
            draft.publisher,
            draft.published_at,
            draft.fetched_at,
            draft.extractor_version,
            draft.content_hash,
            len(draft.text.splitlines()),
            len(draft.text.encode()),
            draft.status,
            draft.error_code,
            draft.fetched_at,
        )
        self.pages[draft.id] = source, tuple(draft.text.splitlines())
        return source

    def seed(self, **changes):
        text = "# Results\nA saved result."
        source = replace(
            WebSource(
                str(uuid4()),
                OWNER,
                SESSION,
                RUN,
                str(uuid4()),
                0,
                URL,
                URL,
                None,
                "Saved paper",
                "Publisher",
                None,
                "2026-10-31T00:00:00Z",
                "trellis-extract-v1",
                hashlib.sha256(text.encode()).hexdigest(),
                2,
                len(text.encode()),
                SourceStatus.SUCCESS,
                None,
                "2026-10-31T00:00:00Z",
            ),
            **changes,
        )
        self.pages[source.id] = source, tuple(text.splitlines())
        return source


class Reader:
    def __init__(self, render=False):
        self.reads = []
        self.render = render

    async def read_document(self, request, context):
        self.reads.append(request.url)
        await asyncio.sleep(0)
        return ExtractedDocumentResult(
            SourceStatus.SUCCESS,
            request.url,
            request.url,
            media_type="text/plain",
            text="# Results\nFresh result.",
            usage=WebUsage(
                page_attempts=1,
                network_requests=1,
                response_bytes=100,
                extracted_bytes=len(b"# Results\nFresh result."),
            ),
            extractor_version="trellis-render-v1" if self.render else "trellis-extract-v1",
        )

    async def render_document(self, request, context):
        return await self.read_document(request, context)


class ResearchSourceReuseTests(unittest.IsolatedAsyncioTestCase):
    def harness(self, active=None):
        repository = Repository()
        reader, renderer = Reader(), Reader(render=True)

        async def get_session(session):
            return SimpleNamespace(user_id=OWNER) if session == SESSION else None

        evidence = WebEvidenceService(
            cast(WebRepository, repository),
            cast(SessionRepository, SimpleNamespace(get_session=get_session)),
            owner_id=OWNER,
        )
        service = WebToolService(
            evidence,
            retriever=cast(DocumentRetriever, reader),
            renderer=cast(BrowserRenderer, renderer),
        )

        async def available():
            return None

        context = ToolExecutionContext(
            OWNER,
            SESSION,
            RUN,
            str(uuid4()),
            str(uuid4()),
            1,
            0,
            datetime.now(UTC) + timedelta(seconds=60),
            limits_for_preset("conservative"),
            None,
            active or available,
            run_mode="research",
            research_policy_version=RESEARCH_POLICY_V2,
        )
        return repository, reader, renderer, service, context

    async def read(self, service, context, *, url=URL, render=False):
        result, failed = await service.execute(
            "web_read", {"urls": [url], "render": render}, context
        )
        self.assertFalse(failed)
        return json.loads(result)["pages"][0]

    async def test_new_tool_call_reuses_same_run_source_before_spending_web_budget(self):
        repository, reader, _, service, context = self.harness()
        saved = repository.seed()
        page = await self.read(service, context)
        self.assertEqual(page["source_id"], saved.id)
        self.assertEqual(page["content_hash"], saved.content_hash)
        self.assertEqual(page["fetched_at"], saved.fetched_at)
        self.assertTrue(page["reused"])
        self.assertTrue(page["reused_run_source"])
        self.assertEqual(reader.reads, [])
        self.assertEqual(repository.reserved, [])
        self.assertEqual(repository.settled, [])

    async def test_exact_final_url_reuses_original_source_identity(self):
        repository, reader, _, service, context = self.harness()
        saved = repository.seed(final_url="https://example.com/final")
        page = await self.read(service, context, url="https://example.com/final")
        self.assertEqual(page["source_id"], saved.id)
        self.assertEqual(reader.reads, [])

    async def test_other_run_is_a_cache_miss_and_legacy_chat_do_not_use_cache(self):
        for mode, policy in (("chat", None), ("research", LEGACY_RESEARCH_POLICY)):
            with self.subTest(mode=mode):
                repository, reader, _, service, context = self.harness()
                repository.seed()
                await self.read(
                    service, replace(context, run_mode=mode, research_policy_version=policy)
                )
                self.assertEqual(reader.reads, [URL])
                self.assertEqual(repository.listed, 0)
        repository, reader, _, service, context = self.harness()
        saved = repository.seed()
        page = await self.read(service, replace(context, run_id=str(uuid4())))
        self.assertNotEqual(page["source_id"], saved.id)
        self.assertIn(saved.id, repository.pages)
        self.assertEqual(reader.reads, [URL])

    async def test_explicit_render_does_not_reuse_static_partial_source(self):
        repository, _, renderer, service, context = self.harness()
        saved = repository.seed(status=SourceStatus.PARTIAL)
        page = await self.read(service, context, render=True)
        self.assertNotEqual(page["source_id"], saved.id)
        self.assertEqual(renderer.reads, [URL])

    async def test_rendered_source_is_reused_even_when_renderer_is_unavailable(self):
        repository, reader, renderer, service, context = self.harness()
        saved = repository.seed(extractor_version="trellis-render-v1+links-v1")
        service._renderer = None
        page = await self.read(service, context, render=True)
        self.assertEqual(page["source_id"], saved.id)
        self.assertEqual(reader.reads + renderer.reads, [])

    async def test_parallel_same_url_requests_share_one_saved_source(self):
        repository, reader, _, service, context = self.harness()
        other = replace(context, tool_call_id=str(uuid4()), dispatch_id=str(uuid4()))
        first, second = await asyncio.gather(self.read(service, context), self.read(service, other))
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertEqual(reader.reads, [URL])
        self.assertEqual(len(repository.reserved), 1)

    async def test_invalid_inventory_or_changed_source_never_reuses_or_fetches(self):
        for field, value in (
            ("user_id", str(uuid4())),
            ("session_id", str(uuid4())),
            ("run_id", str(uuid4())),
            ("evidence_epoch", 1),
        ):
            with self.subTest(field=field):
                repository, reader, _, service, context = self.harness()
                source = repository.seed(**{field: value})
                repository.inventory_override = (source,)
                with self.assertRaises(ApplicationError):
                    await self.read(service, context)
                self.assertEqual(reader.reads, [])
        repository, reader, _, service, context = self.harness()
        saved = repository.seed()
        repository.hydrated_source = replace(saved, content_hash="b" * 64)
        with self.assertRaises(ApplicationError):
            await self.read(service, context)
        self.assertEqual(reader.reads, [])

    async def test_cancellation_and_stale_generation_win_before_cache_return(self):
        cancelled = False

        async def active():
            if cancelled:
                raise ApplicationError("run_cancelled", "Cancelled")

        repository, reader, _, service, context = self.harness(active)
        repository.seed()

        def cancel():
            nonlocal cancelled
            cancelled = True

        repository.after_inventory = cancel
        with self.assertRaises(ApplicationError):
            await self.read(service, context)
        self.assertEqual(reader.reads, [])
        repository, reader, _, service, context = self.harness()
        repository.seed()
        repository.stale = True
        with self.assertRaises(ApplicationError):
            await self.read(service, context)
        self.assertEqual(reader.reads, [])

    async def test_pending_tool_replay_keeps_existing_non_refetch_rule(self):
        repository, reader, _, service, context = self.harness()
        repository.seed()
        page = await self.read(service, replace(context, dispatch_attempt=2))
        self.assertEqual(page["error_code"], "read_not_replayed")
        self.assertEqual(reader.reads, [])
        self.assertEqual(repository.reserved, [])
        original = await self.read(service, context)
        replayed = await self.read(service, replace(context, dispatch_attempt=2))
        self.assertNotEqual(original.get("error_code"), "read_not_replayed")
        self.assertEqual(replayed.get("error_code"), "read_not_replayed")

    async def test_same_tool_completed_source_is_still_replayed_without_fetching(self):
        repository, reader, _, service, context = self.harness()
        original = await self.read(service, context)
        replayed = await self.read(service, replace(context, dispatch_attempt=2))
        self.assertEqual(replayed["source_id"], original["source_id"])
        self.assertTrue(replayed["reused"])
        self.assertEqual(reader.reads, [URL])
        self.assertEqual(len(repository.reserved), 1)

    async def test_canonical_or_different_query_url_does_not_alias_saved_content(self):
        for target in ("https://example.com/canonical", URL + "?edition=2"):
            with self.subTest(target=target):
                repository, reader, _, service, context = self.harness()
                saved = repository.seed(canonical_url="https://example.com/canonical")
                page = await self.read(service, context, url=target)
                self.assertNotEqual(page["source_id"], saved.id)
                self.assertEqual(reader.reads, [target])

    async def test_waiting_request_cancellation_releases_its_lock_reference(self):
        repository, _, _, service, context = self.harness()
        started, release = asyncio.Event(), asyncio.Event()

        class BlockingReader(Reader):
            async def read_document(self, request, context):
                started.set()
                await release.wait()
                return await super().read_document(request, context)

        reader = BlockingReader()
        service._retriever = cast(DocumentRetriever, reader)
        first = asyncio.create_task(self.read(service, context))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(
            self.read(service, replace(context, tool_call_id=str(uuid4())))
        )
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        release.set()
        await first
        self.assertEqual(service._source_read_locks, {})
        self.assertEqual(reader.reads, [URL])
        self.assertEqual(len(repository.reserved), 1)

    async def test_different_urls_can_be_read_concurrently(self):
        _, _, _, service, context = self.harness()
        both_started, release = asyncio.Event(), asyncio.Event()
        started = []

        class BlockingReader(Reader):
            async def read_document(self, request, context):
                started.append(request.url)
                if len(started) == 2:
                    both_started.set()
                await release.wait()
                return await super().read_document(request, context)

        service._retriever = cast(DocumentRetriever, BlockingReader())
        first = asyncio.create_task(self.read(service, context))
        second = asyncio.create_task(
            self.read(service, replace(context, tool_call_id=str(uuid4())), url=URL + "?edition=2")
        )
        try:
            await asyncio.wait_for(both_started.wait(), 1)
        finally:
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(len(started), 2)


if __name__ == "__main__":
    unittest.main()
