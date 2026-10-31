import asyncio
import hashlib
import json
import unittest
from dataclasses import asdict, replace
from typing import cast
from unittest.mock import patch
from uuid import UUID, uuid4

from app.application.web_ports import WebRepository
from app.domain.research import ResearchPhase, ResearchSnapshot
from app.domain.web import SourceLines, SourceStatus, WebSource
from app.infrastructure.web_repository import WebRepositoryMixin

OWNER, SESSION, RUN, TOOL = (str(uuid4()) for _ in range(4))


def snapshot() -> ResearchSnapshot:
    return ResearchSnapshot(
        RUN, SESSION, OWNER, ResearchPhase.WRITING, "Find San Francisco jobs", 0, {}, (), None, 90
    )


def source(lines: tuple[str, ...], **changes) -> WebSource:
    text = "\n".join(lines)
    return replace(
        WebSource(
            str(uuid4()),
            OWNER,
            SESSION,
            RUN,
            TOOL,
            0,
            "https://example.com/jobs",
            "https://example.com/jobs",
            None,
            "Current jobs",
            "Example",
            None,
            "2026-10-31T09:00:00Z",
            "html-v1",
            hashlib.sha256(text.encode()).hexdigest(),
            len(lines),
            len(text.encode()),
            SourceStatus.SUCCESS,
            None,
            "2026-10-31T09:00:00Z",
        ),
        **changes,
    )


class Evidence:
    def __init__(self, pages: tuple[tuple[WebSource, tuple[str, ...]], ...]) -> None:
        self.pages = pages
        self.reads: list[tuple[str, int, int]] = []
        self.changed: dict[str, WebSource | None] = {}

    async def list_run_web_sources(self, session: str, run: str, limit: int = 40):
        if session != SESSION or run != RUN or limit != 40:
            raise AssertionError("wrong run inventory")
        return tuple(page[0] for page in self.pages)

    async def read_web_source(self, session: str, identity: str, start: int, count: int):
        self.reads.append((identity, start, count))
        metadata, lines = next(page for page in self.pages if page[0].id == identity)
        metadata = self.changed.get(identity, metadata)
        if metadata is None:
            return None
        if metadata.status is SourceStatus.CLEARED:
            return SourceLines(metadata, start, (), 0)
        selected = []
        size = 0
        for line in lines[start - 1 : start - 1 + count]:
            if size + len(line.encode()) > 20000:
                break
            selected.append(line)
            size += len(line.encode())
        return SourceLines(metadata, start, tuple(selected), len(lines))


class ResearchEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, web: Evidence):
        from app.application.research_evidence import collect_report_evidence

        return await collect_report_evidence(cast(WebRepository, web), snapshot())

    async def test_saved_pages_survive_empty_worker_checkpoints(self) -> None:
        pages = tuple(
            (
                source((f"Job {index}", "San Francisco", "Apply today")),
                (f"Job {index}", "San Francisco", "Apply today"),
            )
            for index in range(13)
        )
        packet = await self.collect(Evidence(pages))
        self.assertEqual(len(packet["sources"]), 13)
        self.assertEqual(
            packet["sources"][7]["excerpts"],
            [{"start_line": 1, "lines": ["Job 7", "San Francisco", "Apply today"]}],
        )
        self.assertEqual(packet["sources"][7]["source_id"], pages[7][0].id)
        self.assertEqual(packet["omissions"], [])

    async def test_long_pages_include_middle_and_tail_with_stable_line_numbers(self) -> None:
        lines = tuple(f"L{index}: " + "page text " * 7 for index in range(1, 601))
        metadata = source(lines)
        packet = await self.collect(Evidence(((metadata, lines),)))
        excerpts = packet["sources"][0]["excerpts"]
        observed = {
            excerpt["start_line"] + offset: text
            for excerpt in excerpts
            for offset, text in enumerate(excerpt["lines"])
        }
        self.assertIn(1, observed)
        self.assertTrue(any(250 <= line <= 350 for line in observed))
        self.assertIn(600, observed)
        for number, text in observed.items():
            self.assertEqual(text, lines[number - 1])
        self.assertTrue(packet["omissions"])

    async def test_foreign_inventory_is_rejected_before_reading(self) -> None:
        for field in ("user_id", "session_id", "run_id"):
            with self.subTest(field=field):
                metadata = source(("Job",), **{field: str(uuid4())})
                web = Evidence(((metadata, ("Job",)),))
                with self.assertRaises(ValueError):
                    await self.collect(web)
                self.assertEqual(web.reads, [])

    async def test_read_identity_and_hash_must_match_inventory(self) -> None:
        original = source(("Job",))
        for field, value in (
            ("user_id", str(uuid4())),
            ("run_id", str(uuid4())),
            ("id", str(uuid4())),
            ("content_hash", "b" * 64),
        ):
            with self.subTest(field=field):
                web = Evidence(((original, ("Job",)),))
                web.changed[original.id] = replace(original, **{field: value})
                with self.assertRaises(ValueError):
                    await self.collect(web)

    async def test_missing_and_cleared_sources_do_not_leak_stale_metadata(self) -> None:
        original = source(("Job",))
        cleared = replace(
            original,
            requested_url=None,
            final_url=None,
            title=None,
            publisher=None,
            fetched_at=None,
            extractor_version=None,
            content_hash=None,
            line_count=0,
            byte_count=0,
            status=SourceStatus.CLEARED,
        )
        for changed in (None, cleared):
            with self.subTest(cleared=changed is not None):
                web = Evidence(((original, ("Job",)),))
                web.changed[original.id] = changed
                packet = await self.collect(web)
                item = packet["sources"][0]
                self.assertIsNone(item["url"])
                self.assertIsNone(item["title"])
                self.assertEqual(item["excerpts"], [])
                self.assertTrue(packet["omissions"])

    async def test_large_utf8_inventory_stays_bounded_without_cutting_source_lines(self) -> None:
        lines = ("漢" * 2048, "Readable job", "Apply here")
        pages = tuple(
            (
                source(
                    lines,
                    title="漢" * 1000,
                    requested_url="https://example.com/" + "x" * 4000,
                    final_url="https://example.com/" + "x" * 4000,
                ),
                lines,
            )
            for _ in range(40)
        )
        packet = await self.collect(Evidence(pages))
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False).encode()), 48 * 1024)
        self.assertEqual(len(packet["sources"]), 40)
        self.assertTrue(packet["omissions"])
        for item in packet["sources"]:
            for excerpt in item["excerpts"]:
                for number, text in enumerate(excerpt["lines"], excerpt["start_line"]):
                    self.assertEqual(text, lines[number - 1])

    async def test_byte_limited_reads_still_sample_middle_and_last_lines(self) -> None:
        lines = tuple(str(number) + "漢" * 2047 for number in range(40))
        metadata = source(lines)
        web = Evidence(((metadata, lines),))
        packet = await self.collect(web)
        observed = {
            excerpt["start_line"] + offset
            for excerpt in packet["sources"][0]["excerpts"]
            for offset, _ in enumerate(excerpt["lines"])
        }
        self.assertIn(1, observed)
        self.assertTrue(any(18 <= line <= 22 for line in observed))
        self.assertIn(40, observed)
        self.assertLessEqual(len(web.reads), 3)

    async def test_failed_sources_keep_metadata_without_supporting_excerpts(self) -> None:
        metadata = source((), status=SourceStatus.TIMEOUT, content_hash=None)
        web = Evidence(((metadata, ()),))
        packet = await self.collect(web)
        self.assertEqual(packet["sources"][0]["status"], "timeout")
        self.assertEqual(packet["sources"][0]["excerpts"], [])
        self.assertEqual(web.reads, [])

    async def test_read_deadline_preserves_inventory_and_cancels_the_pending_read(self) -> None:
        metadata = source(("Job",))
        cancelled = asyncio.Event()

        class SlowEvidence(Evidence):
            async def read_web_source(self, session: str, identity: str, start: int, count: int):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with patch("app.application.research_evidence._READ_DEADLINE_SECONDS", 0.001):
            packet = await self.collect(SlowEvidence(((metadata, ("Job",)),)))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(packet["sources"][0]["source_id"], metadata.id)
        self.assertEqual(packet["sources"][0]["excerpts"], [])
        self.assertIn("deadline", packet["omissions"][0])

    async def test_cancellation_propagates_from_source_reads(self) -> None:
        class CancelledEvidence(Evidence):
            async def read_web_source(self, session: str, identity: str, start: int, count: int):
                raise asyncio.CancelledError

        metadata = source(("Job",))
        with self.assertRaises(asyncio.CancelledError):
            await self.collect(CancelledEvidence(((metadata, ("Job",)),)))


class Repository(WebRepositoryMixin):
    def __init__(self, rows: list[dict]) -> None:
        self._user_id = UUID(OWNER)
        self.rows = rows
        self.request: tuple[str, str, dict[str, str] | None] | None = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> object:
        self.request = method, path, params
        return self.rows


class RunSourceRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_query_is_owner_session_run_filtered_and_metadata_only(self) -> None:
        metadata = source(("Job",))
        repository = Repository([asdict(metadata)])
        restored = await repository.list_run_web_sources(SESSION, RUN)
        self.assertEqual(restored, (metadata,))
        assert repository.request is not None
        method, path, params = repository.request
        assert params is not None
        self.assertEqual((method, path), ("GET", "web_sources"))
        self.assertEqual(params["user_id"], f"eq.{OWNER}")
        self.assertEqual(params["session_id"], f"eq.{SESSION}")
        self.assertEqual(params["run_id"], f"eq.{RUN}")
        self.assertEqual(params["limit"], "40")
        self.assertEqual(params["order"], "created_at.asc,id.asc")
        self.assertNotIn("text", params["select"].split(","))
        self.assertNotEqual(params["select"], "*")

    async def test_repository_rejects_cross_run_rows_and_oversized_inventory(self) -> None:
        metadata = source(("Job",))
        for rows in ([asdict(replace(metadata, run_id=str(uuid4())))], [asdict(metadata)] * 41):
            with self.subTest(length=len(rows)), self.assertRaises(RuntimeError):
                await Repository(rows).list_run_web_sources(SESSION, RUN)


if __name__ == "__main__":
    unittest.main()
