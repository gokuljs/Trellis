import json
import unittest
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from app.application.errors import ApplicationError
from app.application.research import ResearchGeneration
from app.application.research_ports import ResearchRepository
from app.application.research_reports import ResearchReportService, parse_report, partial_report
from app.application.web_ports import WebRepository
from app.domain.research import ResearchPhase, ResearchReport, ResearchReview, ResearchSnapshot
from app.domain.runtime import RunSnapshot
from app.domain.web import SourceLines, SourceStatus, WebSource

SOURCE = str(uuid4())
SESSION = str(uuid4())
RUN = str(uuid4())
CALL = str(uuid4())


def report_json(claim: str = "Product A costs $10.") -> str:
    reference = {"source_id": SOURCE, "start_line": 1, "end_line": 1}
    return json.dumps(
        {
            "markdown": "## Summary\n\nComparison.\n\n## Findings\n\n"
            + claim
            + f" [[source:{SOURCE}:L1-L1]]\n\n## Conflicts\n\nNone found."
            + "\n\n## Limitations\n\nOnly public pricing was checked.",
            "claims": [{"id": "price", "text": claim, "citations": [reference]}],
            "covered_question_ids": ["pricing"],
            "stop_reason": "coverage_sufficient",
        }
    )


def snapshot() -> ResearchSnapshot:
    return ResearchSnapshot(
        RUN,
        SESSION,
        str(uuid4()),
        ResearchPhase.WRITING,
        "Compare pricing",
        0,
        {"subquestions": [{"id": "pricing", "question": "Compare pricing"}]},
        (),
        None,
        100,
    )


class Evidence:
    def __init__(self, *, available: bool = True, complete: bool = True) -> None:
        self.available = available
        self.complete = complete
        self.reads: list[tuple[str, str, int, int]] = []

    async def read_web_source(
        self, session: str, source: str, start: int, count: int
    ) -> SourceLines | None:
        self.reads.append((session, source, start, count))
        if not self.available:
            return None
        metadata = cast(
            WebSource, SimpleNamespace(status=SourceStatus.SUCCESS, content_hash="a" * 64)
        )
        return SourceLines(metadata, start, ("Product A costs $10.",) if self.complete else (), 1)


class Reports:
    def __init__(self) -> None:
        self.published: list[tuple[ResearchReport, ResearchReview, bool]] = []

    async def publish_research_report(
        self,
        run: str,
        generation: int,
        call: str,
        report: ResearchReport,
        review: ResearchReview,
        *,
        partial: bool = False,
    ) -> None:
        self.published.append((report, review, partial))


class ResearchReportTests(unittest.IsolatedAsyncioTestCase):
    async def run_report(
        self,
        responses: list[str | Exception],
        *,
        evidence: Evidence | None = None,
        state: ResearchSnapshot | None = None,
    ):
        saved = Reports()
        web = evidence or Evidence()
        service = ResearchReportService(cast(WebRepository, web), cast(ResearchRepository, saved))
        purposes: list[str] = []
        self.prompts: dict[str, str] = {}

        async def generate(prompt: str, purpose: str) -> ResearchGeneration:
            purposes.append(purpose)
            self.prompts[purpose] = prompt
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return ResearchGeneration(CALL, response)

        run = cast(RunSnapshot, SimpleNamespace(id=RUN, session_id=SESSION, generation=0))
        await service.finalize(run, state or snapshot(), generate)
        return saved.published, purposes, web

    async def test_publishes_only_after_support_review(self) -> None:
        published, purposes, web = await self.run_report(
            [
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
            ]
        )
        self.assertEqual(purposes, ["draft", "review"])
        report, review, partial = published[0]
        self.assertFalse(partial)
        self.assertEqual(review.report_hash, report.content_hash)
        self.assertEqual(review.source_hashes, {SOURCE: "a" * 64})
        self.assertEqual(web.reads, [(SESSION, SOURCE, 1, 1)])

    async def test_rechecks_repair_and_omits_rejected_claim(self) -> None:
        rejected = json.dumps({"accepted_claim_ids": [], "issues": ["Price is unsupported."]})
        published, purposes, _ = await self.run_report(
            [
                report_json("Product A costs $999."),
                rejected,
                report_json("Product A costs $999."),
                rejected,
            ]
        )
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        report, review, partial = published[0]
        self.assertTrue(partial)
        self.assertNotIn("$999", report.markdown)
        self.assertEqual(report.claims, ())
        self.assertEqual(review.accepted_claim_ids, ())

    async def test_unavailable_evidence_cannot_be_reviewed_or_published_as_claim(self) -> None:
        for evidence in (Evidence(available=False), Evidence(complete=False)):
            with self.subTest(available=evidence.available):
                published, purposes, _ = await self.run_report([report_json()], evidence=evidence)
                self.assertEqual(purposes, ["draft"])
                self.assertEqual(published[0][0].claims, ())
                self.assertTrue(published[0][2])

    async def test_unknown_paid_outcome_propagates_without_review(self) -> None:
        with self.assertRaises(ApplicationError) as raised:
            await self.run_report([ApplicationError("model_outcome_unknown", "Unknown outcome.")])
        self.assertEqual(raised.exception.code, "model_outcome_unknown")

    async def test_uncovered_questions_are_disclosed_before_review(self) -> None:
        state = replace(
            snapshot(),
            checkpoint={
                "subquestions": [
                    {"id": "pricing", "question": "Compare pricing"},
                    {"id": "strategy", "question": "Compare product strategies"},
                ],
                "gaps": ["Private strategy is unavailable"],
                "conflicts": ["Pricing dates differ"],
            },
        )
        published, _, _ = await self.run_report(
            [
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
            ],
            state=state,
        )
        report, review, partial = published[0]
        self.assertTrue(partial)
        self.assertIn("Compare product strategies", report.markdown)
        self.assertIn("did not establish an answer", report.markdown)
        self.assertIn("Private strategy is unavailable", self.prompts["review"])
        self.assertIn("Pricing dates differ", self.prompts["review"])
        self.assertEqual(review.report_hash, report.content_hash)

    def test_rejects_unregistered_and_malformed_citations(self) -> None:
        original = json.loads(report_json())
        for replacement in ("[[source:invalid]]", f"[[source:{uuid4()!s}:L1-L1]]"):
            value = {
                **original,
                "markdown": original["markdown"].replace(f"[[source:{SOURCE}:L1-L1]]", replacement),
            }
            with self.subTest(marker=replacement), self.assertRaises(ValueError):
                parse_report(json.dumps(value))

    def test_missing_sections_are_rejected(self) -> None:
        value = json.loads(report_json())
        value["markdown"] = value["markdown"].replace("## Limitations", "## Notes")
        with self.assertRaises(ValueError):
            parse_report(json.dumps(value))

    def test_partial_report_keeps_only_reviewed_claims(self) -> None:
        report = parse_report(report_json())
        review = ResearchReview(report.content_hash, {SOURCE: "a" * 64}, ("price",), ())
        partial, verdict = partial_report(snapshot(), "time_limit", report, review)
        self.assertEqual(partial.claims, report.claims)
        self.assertEqual(verdict.report_hash, partial.content_hash)
        self.assertNotEqual(verdict.report_hash, review.report_hash)
        self.assertEqual(asdict(verdict)["source_hashes"], {SOURCE: "a" * 64})


if __name__ == "__main__":
    unittest.main()
