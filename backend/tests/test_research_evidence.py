import asyncio
import hashlib
import json
import unittest
from dataclasses import asdict, replace
from typing import cast
from unittest.mock import patch
from uuid import UUID, uuid4

from app.application.web_ports import WebRepository
from app.domain.research import ResearchPhase, ResearchSnapshot, ResearchTask, ResearchTaskStatus
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
    async def test_conflict_citations_pin_evidence_even_without_a_positive_finding(self) -> None:
        from app.application.research_evidence import collect_report_evidence

        lines = tuple(f"Line {number}" for number in range(1, 1601))
        metadata = source(lines)
        state = replace(
            snapshot(),
            checkpoint={
                "conflicts": [
                    {
                        "id": "different-protocols",
                        "text": "The protocols differ.",
                        "question_ids": ["evaluation"],
                        "status": "open",
                        "citations": [
                            {"source_id": metadata.id, "start_line": 1251, "end_line": 1254}
                        ],
                    }
                ]
            },
        )
        packet: dict = await collect_report_evidence(
            cast(WebRepository, Evidence(((metadata, lines),))), state
        )
        excerpt = next(item for item in packet["sources"] if item["source_id"] == metadata.id)
        numbered = {
            group["start_line"] + offset: text
            for group in excerpt["excerpts"]
            for offset, text in enumerate(group["lines"])
        }
        self.assertEqual([numbered[n] for n in range(1251, 1255)], list(lines[1250:1254]))
        self.assertIn(
            "conflicts:different-protocols",
            [item["finding_id"] for item in packet["admitted_findings"]],
        )

    async def collect(self, web: Evidence, checkpoint: dict | None = None):
        from app.application.research_evidence import collect_report_evidence

        return await collect_report_evidence(
            cast(WebRepository, web), replace(snapshot(), checkpoint=checkpoint or {})
        )

    async def test_finding_citations_pin_complete_late_document_evidence(self) -> None:
        lines = tuple(f"Section line {number}: " + "background " * 12 for number in range(2000))
        metadata = source(lines)
        checkpoint: dict[str, object] = {
            "subquestions": [{"id": "methods", "question": "Compare the methods"}],
            "findings": [
                {
                    "id": "late-method",
                    "text": "The method uses a held-out control.",
                    "question_ids": ["methods"],
                    "citations": [{"source_id": metadata.id, "start_line": 1251, "end_line": 1254}],
                }
            ],
        }
        packet = await self.collect(Evidence(((metadata, lines),)), checkpoint)
        observed = {
            excerpt["start_line"] + offset: text
            for excerpt in packet["sources"][0]["excerpts"]
            for offset, text in enumerate(excerpt["lines"])
        }
        self.assertEqual([observed[number] for number in range(1251, 1255)], list(lines[1250:1254]))
        self.assertEqual(packet["admitted_findings"][0]["finding_id"], "late-method")
        self.assertEqual(packet["omitted_findings"], [])

    async def test_overlapping_citations_preserve_both_conflicting_findings_without_duplicate_lines(
        self,
    ) -> None:
        lines = tuple(f"Result line {number}" for number in range(1000))
        metadata = source(lines)
        checkpoint = {
            "subquestions": [{"id": "results", "question": "Compare conflicting results"}],
            "findings": [
                {
                    "id": identity,
                    "text": text,
                    "question_ids": ["results"],
                    "citations": [{"source_id": metadata.id, "start_line": start, "end_line": end}],
                }
                for identity, text, start, end in (
                    ("higher", "One condition reports higher accuracy.", 701, 705),
                    ("lower", "A different condition reports lower accuracy.", 704, 708),
                )
            ],
        }
        packet = await self.collect(Evidence(((metadata, lines),)), checkpoint)
        numbers = [
            excerpt["start_line"] + offset
            for excerpt in packet["sources"][0]["excerpts"]
            for offset, _ in enumerate(excerpt["lines"])
        ]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertTrue(set(range(701, 709)).issubset(numbers))
        self.assertEqual(
            {item["finding_id"] for item in packet["admitted_findings"]}, {"higher", "lower"}
        )

    async def test_one_unavailable_reference_omits_the_whole_finding(self) -> None:
        available, missing = source(("Observed result",)), source(("Missing comparison",))
        web = Evidence(((available, ("Observed result",)), (missing, ("Missing comparison",))))
        web.changed[missing.id] = None
        checkpoint = {
            "findings": [
                {
                    "id": "comparison",
                    "text": "The systems differ.",
                    "question_ids": ["comparison"],
                    "citations": [
                        {"source_id": page.id, "start_line": 1, "end_line": 1}
                        for page in (available, missing)
                    ],
                }
            ]
        }
        packet = await self.collect(web, checkpoint)
        self.assertEqual(packet["admitted_findings"], [])
        self.assertEqual(packet["omitted_findings"][0]["finding_id"], "comparison")
        self.assertIn("unavailable", packet["omitted_findings"][0]["reason"])
        self.assertIsNone(packet["sources"][1]["url"])

    async def test_full_support_has_priority_over_equal_source_sampling_allowances(self) -> None:
        lines = tuple("漢" * 1024 for _ in range(6))
        metadata = source(lines)
        pages = (
            (metadata, lines),
            *((source(("Unrelated background",)), ("Unrelated background",)) for _ in range(39)),
        )
        checkpoint = {
            "subquestions": [{"id": "details", "question": "What do the details show?"}],
            "findings": [
                {
                    "id": "complete-details",
                    "text": "The detailed results cover all six conditions.",
                    "question_ids": ["details"],
                    "citations": [{"source_id": metadata.id, "start_line": 1, "end_line": 6}],
                }
            ],
        }
        packet = await self.collect(Evidence(pages), checkpoint)
        self.assertEqual(
            packet["sources"][0]["excerpts"], [{"start_line": 1, "lines": list(lines)}]
        )
        self.assertEqual(packet["admitted_findings"][0]["finding_id"], "complete-details")
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False).encode()), 48 * 1024)

    async def test_oversized_support_is_disclosed_without_partial_admission(self) -> None:
        lines = tuple("漢" * 2048 for _ in range(20))
        metadata = source(lines)
        checkpoint = {
            "findings": [
                {
                    "id": "too-large",
                    "text": "A claim requiring all twenty lines.",
                    "question_ids": ["details"],
                    "citations": [{"source_id": metadata.id, "start_line": 1, "end_line": 20}],
                }
            ]
        }
        packet = await self.collect(Evidence(((metadata, lines),)), checkpoint)
        self.assertEqual(packet["admitted_findings"], [])
        self.assertEqual(packet["omitted_findings"][0]["finding_id"], "too-large")
        self.assertTrue(packet["omitted_findings"][0]["reason"])
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False).encode()), 48 * 1024)

    async def test_limited_input_prioritizes_coverage_across_requested_questions(self) -> None:
        lines = tuple("漢" * 1024 for _ in range(6))
        pages = tuple((source(lines), lines) for _ in range(3))
        checkpoint = {
            "subquestions": [
                {"id": "first", "question": "Explain the first dimension"},
                {"id": "second", "question": "Explain the second dimension"},
            ],
            "findings": [
                {
                    "id": identity,
                    "text": "A substantial supported finding.",
                    "question_ids": [question],
                    "citations": [{"source_id": page.id, "start_line": 1, "end_line": 6}],
                }
                for identity, question, (page, _) in zip(
                    ("first-a", "first-b", "second-a"),
                    ("first", "first", "second"),
                    pages,
                    strict=True,
                )
            ],
        }
        packet = await self.collect(Evidence(pages), checkpoint)
        self.assertEqual(
            {item["finding_id"] for item in packet["admitted_findings"]},
            {"first-a", "second-a"},
        )
        self.assertEqual(packet["omitted_findings"][0]["finding_id"], "first-b")
        self.assertIn("input limit", packet["omitted_findings"][0]["reason"])
        for item in (packet["sources"][0], packet["sources"][2]):
            self.assertEqual(item["excerpts"], [{"start_line": 1, "lines": list(lines)}])
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False).encode()), 48 * 1024)

    async def test_worker_checkpoint_findings_survive_a_summary_only_result(self) -> None:
        from app.application.research_evidence import collect_report_evidence

        metadata = source(("First line", "Methods use stratified sampling", "Last line"))
        task_id = str(uuid4())
        checkpoint: dict[str, object] = {
            "findings": [
                {
                    "id": "worker-method",
                    "text": "The method uses stratified sampling.",
                    "question_ids": ["methods"],
                    "citations": [{"source_id": metadata.id, "start_line": 2, "end_line": 2}],
                }
            ]
        }
        task = ResearchTask(
            task_id,
            RUN,
            "Read the methods",
            "worker",
            1,
            ResearchTaskStatus.COMPLETED,
            1,
            checkpoint,
            {"summary": "Completed the assignment"},
        )
        web = Evidence(
            ((metadata, ("First line", "Methods use stratified sampling", "Last line")),)
        )
        packet: dict = await collect_report_evidence(
            cast(WebRepository, web), replace(snapshot(), tasks=(task,))
        )
        self.assertEqual(packet["admitted_findings"][0]["finding_id"], "worker-method")
        self.assertEqual(packet["admitted_findings"][0]["task_id"], task_id)

    async def test_source_outside_run_inventory_cannot_support_a_finding(self) -> None:
        metadata = source(("Owned evidence",))
        checkpoint = {
            "findings": [
                {
                    "id": "foreign-source",
                    "text": "Unsupported cross-run assertion.",
                    "citations": [{"source_id": str(uuid4()), "start_line": 1, "end_line": 1}],
                }
            ]
        }
        web = Evidence(((metadata, ("Owned evidence",)),))
        packet = await self.collect(web, checkpoint)
        self.assertEqual(packet["admitted_findings"], [])
        self.assertEqual(packet["omitted_findings"][0]["finding_id"], "foreign-source")
        self.assertTrue(all(identity == metadata.id for identity, _, _ in web.reads))

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
