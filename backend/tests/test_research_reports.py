import json
import unittest
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from app.application.budgets import BudgetExceeded
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
OWNER = str(uuid4())
TOOL = str(uuid4())


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
        OWNER,
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

    def metadata(self) -> WebSource:
        return WebSource(
            SOURCE,
            OWNER,
            SESSION,
            RUN,
            TOOL,
            0,
            "https://example.com/pricing",
            "https://example.com/pricing",
            None,
            "Published pricing",
            "Product A",
            None,
            "2026-10-31T10:00:00Z",
            "html-v1",
            "a" * 64,
            1,
            23,
            SourceStatus.SUCCESS,
            None,
            "2026-10-31T10:00:00Z",
        )

    async def list_run_web_sources(
        self, session_id: str, run_id: str, limit: int = 40
    ) -> tuple[WebSource, ...]:
        return (self.metadata(),)

    async def read_web_source(
        self, session: str, source: str, start: int, count: int
    ) -> SourceLines | None:
        self.reads.append((session, source, start, count))
        if not self.available:
            return None
        metadata = self.metadata()
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
        responses: list[str | Exception | ResearchGeneration],
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
            if isinstance(response, ResearchGeneration):
                return response
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
        self.assertEqual(web.reads[-1], (SESSION, SOURCE, 1, 1))

    async def test_invalid_draft_uses_saved_evidence_and_the_repair_allowance(self) -> None:
        invalid = json.loads(report_json())
        invalid["claims"][0]["citations"] = []
        state = replace(
            snapshot(),
            checkpoint={
                "findings": [],
                "examined_source_ids": [],
                "subquestions": [{"id": "pricing", "question": "Compare pricing"}],
                "stop_reason": "research_finalization_reserve",
            },
        )
        published, purposes, _ = await self.run_report(
            [
                json.dumps(invalid),
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
            ],
            state=state,
        )
        self.assertEqual(purposes, ["draft", "repair", "recheck"])
        self.assertFalse(published[0][2])
        self.assertIn(SOURCE, self.prompts["draft"])
        self.assertIn("Product A costs $10.", self.prompts["draft"])
        self.assertIn("supporting citations", self.prompts["repair"])
        self.assertIn("covered_question_ids", self.prompts["repair"])

    async def test_invalid_json_and_sections_can_be_repaired_once(self) -> None:
        invalid_sections = json.loads(report_json())
        invalid_sections["markdown"] = invalid_sections["markdown"].replace(
            "## Limitations", "## Notes"
        )
        for invalid in ("invalid JSON", json.dumps(invalid_sections)):
            with self.subTest(response=invalid):
                published, purposes, _ = await self.run_report(
                    [
                        invalid,
                        report_json(),
                        json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
                    ]
                )
                self.assertEqual(purposes, ["draft", "repair", "recheck"])
                self.assertFalse(published[0][2])

    async def test_invalid_review_uses_the_same_single_repair_allowance(self) -> None:
        published, purposes, _ = await self.run_report(
            [
                report_json(),
                "invalid JSON",
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
            ]
        )
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        self.assertFalse(published[0][2])

    async def test_output_limited_draft_is_repaired_with_reduced_scope(self) -> None:
        for content in ('{"markdown": "truncated', report_json()):
            with self.subTest(content=content):
                published, purposes, _ = await self.run_report(
                    [
                        ResearchGeneration(CALL, content, incomplete=True),
                        report_json(),
                        json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
                    ]
                )
                self.assertEqual(purposes, ["draft", "repair", "recheck"])
                self.assertFalse(published[0][2])
                self.assertIn("output limit", self.prompts["repair"])
                self.assertIn("number of claims", self.prompts["repair"])
                self.assertIn("reduce report scope", self.prompts["repair"])

    async def test_output_limited_review_cannot_accept_claims(self) -> None:
        published, purposes, _ = await self.run_report(
            [
                report_json(),
                ResearchGeneration(
                    CALL,
                    json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
                    incomplete=True,
                ),
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
            ]
        )
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        self.assertFalse(published[0][2])
        self.assertIn("review response reached its output limit", self.prompts["repair"])

    async def test_output_limited_repair_stops_without_another_candidate(self) -> None:
        published, purposes, _ = await self.run_report(
            ["invalid JSON", ResearchGeneration(CALL, report_json(), incomplete=True)]
        )
        report, review, partial = published[0]
        self.assertEqual(purposes, ["draft", "repair"])
        self.assertTrue(partial)
        self.assertEqual(report.claims, ())
        self.assertEqual(review.accepted_claim_ids, ())
        self.assertIn("report response reached its output limit", report.markdown)
        self.assertIn("https://example.com/pricing", report.markdown)

    async def test_output_limited_repair_keeps_only_prior_reviewed_claims(self) -> None:
        published, purposes, _ = await self.run_report(
            [
                report_json(),
                json.dumps(
                    {"accepted_claim_ids": ["price"], "issues": ["Disclose limited scope."]}
                ),
                ResearchGeneration(CALL, report_json("Product A costs $999."), incomplete=True),
            ]
        )
        report, review, partial = published[0]
        self.assertEqual(purposes, ["draft", "review", "repair"])
        self.assertTrue(partial)
        self.assertEqual(report.claims, parse_report(report_json()).claims)
        self.assertNotIn("$999", report.markdown)
        self.assertEqual(review.accepted_claim_ids, ("price",))
        self.assertEqual(review.report_hash, report.content_hash)
        self.assertEqual(review.source_hashes, {SOURCE: "a" * 64})

    async def test_output_limited_recheck_keeps_the_prior_review(self) -> None:
        published, purposes, _ = await self.run_report(
            [
                report_json(),
                json.dumps(
                    {"accepted_claim_ids": ["price"], "issues": ["Disclose limited scope."]}
                ),
                report_json("Product A costs $999."),
                ResearchGeneration(
                    CALL,
                    json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
                    incomplete=True,
                ),
            ]
        )
        report, review, partial = published[0]
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        self.assertTrue(partial)
        self.assertEqual(report.claims, parse_report(report_json()).claims)
        self.assertNotIn("$999", report.markdown)
        self.assertIn("review response reached its output limit", report.markdown)
        self.assertEqual(review.report_hash, report.content_hash)

    async def test_output_limit_does_not_retry_after_unknown_repair_outcome(self) -> None:
        with self.assertRaises(ApplicationError) as raised:
            await self.run_report(
                [
                    ResearchGeneration(CALL, report_json(), incomplete=True),
                    ApplicationError("model_outcome_unknown", "Unknown outcome."),
                ]
            )
        self.assertEqual(raised.exception.code, "model_outcome_unknown")
        self.assertEqual(set(self.prompts), {"draft", "repair"})

    async def test_output_limit_does_not_retry_after_repair_budget_exhaustion(self) -> None:
        published, purposes, _ = await self.run_report(
            [
                ResearchGeneration(CALL, report_json(), incomplete=True),
                BudgetExceeded("research_finalization_limit"),
            ]
        )
        self.assertEqual(purposes, ["draft", "repair"])
        self.assertTrue(published[0][2])
        self.assertEqual(published[0][0].stop_reason, "research_finalization_limit")
        self.assertEqual(published[0][0].claims, ())

    async def test_failed_repair_preserves_saved_sources_without_unreviewed_claims(self) -> None:
        published, purposes, _ = await self.run_report(["invalid JSON", "invalid JSON"])
        report, review, partial = published[0]
        self.assertEqual(purposes, ["draft", "repair"])
        self.assertTrue(partial)
        self.assertEqual(report.claims, ())
        self.assertEqual(review.accepted_claim_ids, ())
        self.assertIn("https://example.com/pricing", report.markdown)
        self.assertIn("Collected sources", report.markdown)
        self.assertIn("not valid JSON", report.markdown)

    async def test_invalid_repair_keeps_only_claims_reviewed_in_the_prior_revision(self) -> None:
        published, purposes, _ = await self.run_report(
            [
                report_json(),
                json.dumps(
                    {"accepted_claim_ids": ["price"], "issues": ["Disclose limited scope."]}
                ),
                "invalid JSON",
            ]
        )
        report, review, partial = published[0]
        self.assertEqual(purposes, ["draft", "review", "repair"])
        self.assertTrue(partial)
        self.assertEqual([claim.id for claim in report.claims], ["price"])
        self.assertEqual(review.report_hash, report.content_hash)

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
                published, purposes, _ = await self.run_report(
                    [report_json(), report_json()], evidence=evidence
                )
                self.assertEqual(purposes, ["draft", "repair"])
                self.assertEqual(published[0][0].claims, ())
                self.assertTrue(published[0][2])

    async def test_unknown_paid_outcome_propagates_without_review(self) -> None:
        with self.assertRaises(ApplicationError) as raised:
            await self.run_report([ApplicationError("model_outcome_unknown", "Unknown outcome.")])
        self.assertEqual(raised.exception.code, "model_outcome_unknown")

    async def test_budget_exhaustion_during_review_starts_no_repair(self) -> None:
        published, purposes, _ = await self.run_report(
            [report_json(), BudgetExceeded("research_finalization_limit")]
        )
        self.assertEqual(purposes, ["draft", "review"])
        self.assertTrue(published[0][2])
        self.assertEqual(published[0][0].stop_reason, "research_finalization_limit")
        self.assertEqual(published[0][0].claims, ())

    async def test_unknown_outcome_during_repair_starts_no_recheck(self) -> None:
        with self.assertRaises(ApplicationError) as raised:
            await self.run_report(
                ["invalid JSON", ApplicationError("model_outcome_unknown", "Unknown outcome.")]
            )
        self.assertEqual(raised.exception.code, "model_outcome_unknown")
        self.assertEqual(set(self.prompts), {"draft", "repair"})

    async def test_invalid_recheck_does_not_start_another_repair(self) -> None:
        published, purposes, _ = await self.run_report(
            [report_json(), "invalid JSON", report_json(), "invalid JSON"]
        )
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        self.assertTrue(published[0][2])
        self.assertEqual(published[0][0].claims, ())

    async def test_oversized_complete_evidence_is_repaired_before_paid_review(self) -> None:
        class LargeEvidence(Evidence):
            def metadata(self) -> WebSource:
                return replace(super().metadata(), line_count=6, byte_count=108_000)

            async def read_web_source(self, session, source, start, count):
                self.reads.append((session, source, start, count))
                return SourceLines(
                    self.metadata(), start, ("Product A costs $10.".ljust(18_000),), 6
                )

        large = json.loads(report_json())
        references = [
            {"source_id": SOURCE, "start_line": line, "end_line": line} for line in range(1, 7)
        ]
        large["claims"][0]["citations"] = references
        large["markdown"] += "\n" + " ".join(
            f"[[source:{SOURCE}:L{line}-L{line}]]" for line in range(2, 7)
        )
        published, purposes, _ = await self.run_report(
            [
                json.dumps(large),
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
            ],
            evidence=LargeEvidence(),
        )
        self.assertEqual(purposes, ["draft", "repair", "recheck"])
        self.assertFalse(published[0][2])
        self.assertIn("reduce report scope", self.prompts["repair"])

    async def test_cross_run_citations_are_rejected_before_any_paid_review(self) -> None:
        class ChangedEvidence(Evidence):
            async def read_web_source(self, session, source, start, count):
                result = await super().read_web_source(session, source, start, count)
                assert result is not None
                if len(self.reads) > 1:
                    return replace(result, source=replace(result.source, run_id=str(uuid4())))
                return result

        published, purposes, _ = await self.run_report(
            [report_json(), report_json()], evidence=ChangedEvidence()
        )
        self.assertEqual(purposes, ["draft", "repair"])
        self.assertTrue(published[0][2])
        self.assertEqual(published[0][0].claims, ())
        self.assertIn("do not belong to this research run", published[0][0].markdown)

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

    def test_partial_report_cannot_reuse_a_review_for_another_revision(self) -> None:
        report = parse_report(report_json())
        review = ResearchReview("b" * 64, {SOURCE: "a" * 64}, ("price",), ())
        partial, verdict = partial_report(snapshot(), "time_limit", report, review)
        self.assertEqual(partial.claims, ())
        self.assertEqual(verdict.accepted_claim_ids, ())

    def test_partial_source_links_cannot_introduce_citation_markers_or_unsafe_schemes(self) -> None:
        evidence: dict[str, object] = {
            "sources": [
                {
                    "title": f"[[source:{SOURCE}:L1-L1]]",
                    "url": "javascript:alert(1)",
                    "status": "success",
                }
            ]
        }
        partial, _review = partial_report(snapshot(), "report_validation_failed", evidence=evidence)
        self.assertNotIn("[[source:", partial.markdown)
        self.assertNotIn("javascript:", partial.markdown)
        evidence["sources"] = [
            {
                "title": "Recorded source",
                "url": f"https://example.com/[[source:{SOURCE}:L1-L1]]",
                "status": "success",
            }
        ]
        partial, _review = partial_report(snapshot(), "report_validation_failed", evidence=evidence)
        self.assertNotIn("[[source:", partial.markdown)
        self.assertIn("%5B%5Bsource", partial.markdown)


if __name__ == "__main__":
    unittest.main()
