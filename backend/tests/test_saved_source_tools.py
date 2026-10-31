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
from app.application.tool_context import ToolExecutionContext
from app.application.web_evidence import WebEvidenceService
from app.application.web_ports import WebRepository
from app.application.web_tools import WebToolService
from app.domain.web import SourceLines, SourceStatus, WebRunState, WebSource, WebUsage

OWNER, SESSION, RUN, TOOL, DISPATCH, SOURCE = (str(uuid4()) for _ in range(6))


class SavedSources:
    def __init__(self, lines: tuple[str, ...]) -> None:
        self.lines = lines
        text = "\n".join(lines)
        self.source = WebSource(
            SOURCE,
            OWNER,
            SESSION,
            RUN,
            TOOL,
            0,
            "https://example.com/paper",
            "https://example.com/paper",
            None,
            "A paper",
            "Example",
            None,
            "2026-10-31T00:00:00Z",
            "text-v1",
            hashlib.sha256(text.encode()).hexdigest(),
            len(lines),
            len(text.encode()),
            SourceStatus.SUCCESS,
            None,
            "2026-10-31T00:00:00Z",
        )
        self.reads: list[tuple[int, int]] = []

    async def open_web_run(self, dispatch):
        return WebRunState(0, WebUsage())

    async def read_web_source(self, session, source, start, count):
        if session != SESSION or source != SOURCE:
            return None
        self.reads.append((start, count))
        chosen = []
        size = 0
        for line in self.lines[start - 1 : start - 1 + count]:
            if size + len(line.encode()) > 20000:
                break
            chosen.append(line)
            size += len(line.encode())
        return SourceLines(self.source, start, tuple(chosen), len(self.lines))


class Sessions:
    async def get_session(self, session):
        return SimpleNamespace(user_id=OWNER) if session == SESSION else None


class SavedSourceToolTests(unittest.IsolatedAsyncioTestCase):
    def service(self, lines: tuple[str, ...]):
        repository = SavedSources(lines)
        evidence = WebEvidenceService(
            cast(WebRepository, repository), cast(SessionRepository, Sessions()), owner_id=OWNER
        )
        return WebToolService(evidence), repository

    def context(self, active=None):
        async def available():
            return None

        return ToolExecutionContext(
            OWNER,
            SESSION,
            RUN,
            TOOL,
            DISPATCH,
            1,
            0,
            datetime.now(UTC) + timedelta(seconds=60),
            limits_for_preset("conservative"),
            None,
            active or available,
        )

    async def execute(self, service, name, arguments, context=None):
        output, failed = await service.execute(name, arguments, context or self.context())
        self.assertFalse(failed)
        return json.loads(output)

    async def test_research_tools_are_opt_in_without_changing_legacy_contracts(self):
        service, _ = self.service(("# Abstract", "Text"))
        legacy = service.registrations(search_ready=False, read_ready=False)
        research = service.registrations(search_ready=False, read_ready=False, research_ready=True)
        self.assertEqual([item.name for item in legacy], ["web_source"])
        self.assertEqual(
            {item.name for item in research},
            {"web_source", "web_source_read", "web_source_find", "web_source_outline"},
        )
        self.assertEqual(research[0], legacy[0])

    async def test_outline_finds_late_sections_with_exact_source_identity(self):
        lines = (
            ("# Abstract",)
            + ("body paragraph.",) * 1200
            + ("## 4 Results", "A measured result.", "## Limitations", "Uncertain generality.")
        )
        service, repository = self.service(lines)
        value = await self.execute(service, "web_source_outline", {"source_id": SOURCE})
        self.assertEqual(value["content_hash"], repository.source.content_hash)
        self.assertEqual(value["source_id"], SOURCE)
        self.assertIn({"line": 1202, "text": "## 4 Results"}, value["headings"])
        self.assertTrue(value["scan_complete"])
        self.assertFalse(value["substantive"])
        self.assertTrue(all(count <= 200 for _, count in repository.reads))

    async def test_find_is_literal_and_returns_complete_numbered_context(self):
        service, repository = self.service(
            ("# Results", "Before", "Accuracy [a+b] was 89%.", "After", "aab is unrelated.")
        )
        value = await self.execute(
            service,
            "web_source_find",
            {
                "source_id": SOURCE,
                "query": "[A+B]",
                "context_lines": 1,
            },
        )
        self.assertEqual(
            value["matches"],
            [
                {
                    "match_line": 3,
                    "start_line": 2,
                    "untrusted_lines": [
                        {"line": 2, "text": "Before"},
                        {"line": 3, "text": "Accuracy [a+b] was 89%."},
                        {"line": 4, "text": "After"},
                    ],
                }
            ],
        )
        self.assertEqual(value["presented_ranges"], [{"start_line": 2, "end_line": 4}])
        self.assertEqual(value["content_hash"], repository.source.content_hash)
        self.assertFalse(value["substantive"])

    async def test_scan_has_bounded_continuation_instead_of_silent_omissions(self):
        service, _ = self.service(("body",) * 2200 + ("# Late results",))
        first = await self.execute(
            service,
            "web_source_find",
            {
                "source_id": SOURCE,
                "query": "Late",
                "max_scan_lines": 1000,
            },
        )
        self.assertEqual(first["matches"], [])
        self.assertFalse(first["scan_complete"])
        self.assertEqual(first["next_scan_line"], 1001)
        final = await self.execute(
            service,
            "web_source_find",
            {
                "source_id": SOURCE,
                "query": "Late",
                "start_line": 2001,
            },
        )
        self.assertEqual(final["matches"][0]["match_line"], 2201)
        self.assertTrue(final["scan_complete"])

    async def test_read_marks_only_the_complete_lines_presented_as_substantive(self):
        service, _ = self.service(("Abstract", "Methods", "Results", "Limits"))
        value = await self.execute(
            service,
            "web_source_read",
            {
                "source_id": SOURCE,
                "start_line": 2,
                "max_lines": 2,
            },
        )
        self.assertEqual(
            value["untrusted_lines"],
            [{"line": 2, "text": "Methods"}, {"line": 3, "text": "Results"}],
        )
        self.assertEqual(value["presented_ranges"], [{"start_line": 2, "end_line": 3}])
        self.assertTrue(value["substantive"])
        self.assertTrue(value["has_more"])

    async def test_foreign_run_and_missing_source_cannot_be_inspected(self):
        service, repository = self.service(("private",))
        for change in (
            {"id": str(uuid4())},
            {"run_id": str(uuid4())},
            {"user_id": str(uuid4())},
            {"session_id": str(uuid4())},
        ):
            repository.source = replace(repository.source, **change)
            value = await self.execute(
                service,
                "web_source_find",
                {
                    "source_id": SOURCE,
                    "query": "private",
                },
            )
            self.assertEqual(value["error_code"], "source_not_found")
            self.assertNotIn("private", json.dumps(value))
            repository.source = replace(
                repository.source,
                **{
                    "id": SOURCE,
                    "run_id": RUN,
                    "user_id": OWNER,
                    "session_id": SESSION,
                },
            )

    async def test_cancelled_scan_stops_before_reading_the_whole_source(self):
        service, repository = self.service(("body",) * 2200)
        calls = 0

        async def cancelled():
            nonlocal calls
            calls += 1
            if calls == 5:
                raise ApplicationError("run_cancelled", "Cancelled")

        with self.assertRaisesRegex(ApplicationError, "Cancelled"):
            await self.execute(
                service, "web_source_outline", {"source_id": SOURCE}, self.context(cancelled)
            )
        self.assertLess(len(repository.reads), 4)

    async def test_large_matches_keep_valid_complete_evidence_and_resume_cursor(self):
        line = "MATCH " + "界" * 1800
        service, _ = self.service((line,) * 30)
        value = await self.execute(
            service,
            "web_source_find",
            {
                "source_id": SOURCE,
                "query": "MATCH",
                "context_lines": 0,
                "max_matches": 20,
            },
        )
        self.assertLess(len(json.dumps(value, ensure_ascii=False).encode()), 20000)
        self.assertTrue(value["matches"])
        self.assertFalse(value["scan_complete"])
        self.assertGreater(value["next_scan_line"], 1)
        for match in value["matches"]:
            self.assertEqual(match["untrusted_lines"][0]["text"], line)

    async def test_oversized_context_keeps_complete_match_instead_of_stalling(self):
        line = "界" * 1800
        service, _ = self.service((line,) * 5 + ("MATCH " + line,) + (line,) * 5)
        value = await self.execute(
            service,
            "web_source_find",
            {
                "source_id": SOURCE,
                "query": "MATCH",
                "context_lines": 5,
            },
        )
        self.assertEqual(
            value["matches"],
            [
                {
                    "match_line": 6,
                    "start_line": 6,
                    "context_omitted": True,
                    "untrusted_lines": [{"line": 6, "text": "MATCH " + line}],
                }
            ],
        )
        self.assertEqual(value["presented_ranges"], [{"start_line": 6, "end_line": 6}])
        self.assertTrue(value["scan_complete"])


if __name__ == "__main__":
    unittest.main()
