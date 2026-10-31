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
from app.application.research_reports import (
    ResearchReportService,
    parse_report,
    parse_review,
    partial_report,
)
from app.application.web_ports import WebRepository
from app.domain.research import (
    ResearchPhase,
    ResearchReport,
    ResearchReview,
    ResearchSnapshot,
    ResearchTask,
    ResearchTaskStatus,
)
from app.domain.runtime import RunSnapshot
from app.domain.web import SourceLines, SourceStatus, WebSource

SOURCE = str(uuid4())
SESSION = str(uuid4())
RUN = str(uuid4())
CALL = str(uuid4())
OWNER = str(uuid4())
TOOL = str(uuid4())
LONG_STOP_REASON = (
    "Bounded research collection completed; further searching was not pursued. Coverage is "
    "selective, with practical-resource and publication-status verification remaining incomplete."
)


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

    async def test_narrative_stopping_reason_reaches_review_without_repair(self) -> None:
        value = json.loads(report_json())
        value["stop_reason"] = LONG_STOP_REASON
        published, purposes, _ = await self.run_report(
            [json.dumps(value), json.dumps({"accepted_claim_ids": ["price"], "issues": []})]
        )
        self.assertEqual(purposes, ["draft", "review"])
        report, review, partial = published[0]
        self.assertFalse(partial)
        self.assertEqual(report.stop_reason, "collection_complete")
        self.assertEqual(report.claims, parse_report(report_json()).claims)
        self.assertEqual(report.markdown.count(LONG_STOP_REASON), 1)
        self.assertIn(LONG_STOP_REASON, report.markdown.split("## Limitations", 1)[1])
        reviewed = json.loads(self.prompts["review"].rsplit("\n", 1)[-1])["report"]
        self.assertEqual(reviewed, json.loads(json.dumps(asdict(report))))
        self.assertEqual(review.report_hash, report.content_hash)

    async def test_report_uses_the_saved_runtime_stopping_reason(self) -> None:
        lead = ResearchTask(
            str(uuid4()),
            RUN,
            "Compare pricing",
            "lead",
            0,
            ResearchTaskStatus.COMPLETED,
            0,
            {"stop_reason": "low_expected_value"},
        )
        base = replace(
            snapshot(),
            stop_reason="page_limit",
            checkpoint={**snapshot().checkpoint, "stop_reason": "research_finalization_reserve"},
            tasks=(lead,),
        )
        value = json.loads(report_json())
        del value["stop_reason"]
        for state, expected in (
            (base, "page_limit"),
            (replace(base, stop_reason=None), "research_finalization_reserve"),
            (
                replace(base, stop_reason=None, checkpoint=snapshot().checkpoint),
                "low_expected_value",
            ),
        ):
            with self.subTest(expected=expected):
                published, purposes, _ = await self.run_report(
                    [
                        json.dumps(value),
                        json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
                    ],
                    state=state,
                )
                self.assertEqual(purposes, ["draft", "review"])
                self.assertEqual(published[0][0].stop_reason, expected)

    async def test_legacy_stopping_metadata_preserves_the_reviewed_revision(self) -> None:
        value = json.loads(report_json())
        value["stop_reason"] = "  coverage_sufficient  "
        original = parse_report(json.dumps(value))
        published, purposes, _ = await self.run_report(
            [json.dumps(value), json.dumps({"accepted_claim_ids": ["price"], "issues": []})]
        )
        self.assertEqual(purposes, ["draft", "review"])
        report, review, partial = published[0]
        self.assertFalse(partial)
        self.assertEqual(report, original)
        self.assertEqual(report.stop_reason, "  coverage_sufficient  ")
        self.assertEqual(review.report_hash, original.content_hash)

    async def test_writer_can_omit_runtime_stopping_metadata(self) -> None:
        value = json.loads(report_json())
        del value["stop_reason"]
        published, purposes, _ = await self.run_report(
            [json.dumps(value), json.dumps({"accepted_claim_ids": ["price"], "issues": []})]
        )
        self.assertEqual(purposes, ["draft", "review"])
        self.assertFalse(published[0][2])
        self.assertEqual(published[0][0].stop_reason, "collection_complete")

    async def test_stopping_explanation_still_requires_valid_size_and_citations(self) -> None:
        for explanation in (
            LONG_STOP_REASON + f" [[source:{uuid4()!s}:L1-L1]]",
            "x" * (64 * 1024),
        ):
            with self.subTest(explanation_length=len(explanation)):
                value = json.loads(report_json())
                value["stop_reason"] = explanation
                published, purposes, _ = await self.run_report(
                    [json.dumps(value), json.dumps(value)]
                )
                self.assertEqual(purposes, ["draft", "repair"])
                self.assertTrue(published[0][2])
                self.assertEqual(published[0][0].claims, ())

    def test_direct_report_parser_preserves_the_bounded_metadata_contract(self) -> None:
        value = json.loads(report_json())
        value["stop_reason"] = LONG_STOP_REASON
        with self.assertRaises(ValueError):
            parse_report(json.dumps(value))

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

    async def test_structured_review_preserves_accepted_findings_after_failed_repair(self) -> None:
        issue = {"claim_ids": ["price"], "issue": "Disclose the limited comparison scope."}
        published, purposes, _ = await self.run_report(
            [
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": [issue]}),
                "invalid JSON",
            ]
        )
        report, review, partial = published[0]
        self.assertEqual(purposes, ["draft", "review", "repair"])
        self.assertTrue(partial)
        self.assertEqual(report.claims, parse_report(report_json()).claims)
        self.assertEqual(review.accepted_claim_ids, ("price",))
        self.assertEqual(review.report_hash, report.content_hash)
        repair = json.loads(self.prompts["repair"].rsplit("\n", 1)[-1])
        self.assertEqual(json.loads(repair["review"]["issues"][0]), issue)

    async def test_structured_review_feedback_repairs_the_report(self) -> None:
        issue = {"claim_id": "price", "issue": "The price is $10, not $999."}
        published, purposes, _ = await self.run_report(
            [
                report_json("Product A costs $999."),
                json.dumps({"accepted_claim_ids": [], "issues": [issue]}),
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
            ]
        )
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        self.assertFalse(published[0][2])
        self.assertNotIn("$999", published[0][0].markdown)
        repair = json.loads(self.prompts["repair"].rsplit("\n", 1)[-1])
        self.assertEqual(json.loads(repair["review"]["issues"][0]), issue)

    async def test_review_receives_owned_source_provenance(self) -> None:
        published, purposes, evidence = await self.run_report(
            [report_json(), json.dumps({"accepted_claim_ids": ["price"], "issues": []})]
        )
        self.assertEqual(purposes, ["draft", "review"])
        source = evidence.metadata()
        reviewed = json.loads(self.prompts["review"].rsplit("\n", 1)[-1])["evidence"][0]
        for field in (
            "requested_url",
            "final_url",
            "canonical_url",
            "title",
            "publisher",
            "published_at",
            "fetched_at",
        ):
            self.assertEqual(reviewed[field], getattr(source, field))
        self.assertEqual(reviewed["lines"], ["Product A costs $10."])
        self.assertEqual(published[0][1].source_hashes, {SOURCE: source.content_hash})

    async def test_malformed_review_feedback_is_sent_to_the_single_repair(self) -> None:
        malformed = {"accepted_claim_ids": ["price"], "issues": [{"message": "Wrong shape."}]}
        published, purposes, _ = await self.run_report(
            [
                report_json(),
                json.dumps(malformed),
                report_json(),
                json.dumps({"accepted_claim_ids": ["price"], "issues": []}),
            ]
        )
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        repair = json.loads(self.prompts["repair"].rsplit("\n", 1)[-1])
        self.assertEqual(json.loads(repair["response"]), malformed)
        self.assertEqual(
            repair["report"], json.loads(json.dumps(asdict(parse_report(report_json()))))
        )
        self.assertIn("issue", repair["validation_issue"])
        self.assertFalse(published[0][2])

    def test_review_accepts_bounded_structured_issues_and_preserves_legacy_feedback(self) -> None:
        report = parse_report(report_json())
        hashes = {SOURCE: "a" * 64}
        issues = [
            "  Keep this legacy feedback unchanged.  ",
            {"claim_ids": ["price"], "issue": "Check the comparison scope."},
            {"claim_id": "price", "issue": "Add qualification."},
            {"claim_ids": [], "issue": "The report needs a scope disclosure."},
            {"issue": "Coverage remains incomplete."},
        ]
        review = parse_review(
            json.dumps({"accepted_claim_ids": ["price"], "issues": issues}), report, hashes
        )
        self.assertEqual(review.report_hash, report.content_hash)
        self.assertEqual(review.source_hashes, hashes)
        self.assertEqual(review.accepted_claim_ids, ("price",))
        self.assertEqual(review.issues[0], issues[0])
        self.assertEqual([json.loads(value) for value in review.issues[1:]], issues[1:])

    def test_review_rejects_invalid_or_unbounded_structured_issues(self) -> None:
        report = parse_report(report_json())
        invalid = [
            [{"issue": ""}],
            [{"issue": 5}],
            [{"message": "Wrong field."}],
            [{"issue": "Unknown field.", "severity": "low"}],
            [{"claim_id": "missing", "issue": "Unknown claim."}],
            [{"claim_ids": ["missing"], "issue": "Unknown claims."}],
            [{"claim_ids": "price", "issue": "Wrong collection."}],
            [{"claim_ids": [5], "issue": "Wrong claim type."}],
            [{"claim_id": "price", "claim_ids": [], "issue": "Ambiguous target."}],
            [{"issue": "x" * 8001}],
            [{"issue": "One issue."}] * 101,
        ]
        for issues in invalid:
            with self.subTest(issues=repr(issues)[:150]), self.assertRaises(ValueError):
                parse_review(
                    json.dumps({"accepted_claim_ids": [], "issues": issues}),
                    report,
                    {SOURCE: "a" * 64},
                )

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

    def summary_report(self) -> ResearchReport:
        value = json.loads(report_json())
        text = "Product A has a public price of $10."
        value["markdown"] = value["markdown"].replace(
            "Comparison.", text + f" [[source:{SOURCE}:L1-L1]]"
        )
        value["claims"].insert(
            0,
            {"id": "summary", "text": text, "citations": value["claims"][0]["citations"]},
        )
        return parse_report(json.dumps(value))

    def test_partial_report_preserves_only_an_explicitly_reviewed_summary(self) -> None:
        report = self.summary_report()
        review = ResearchReview(report.content_hash, {SOURCE: "a" * 64}, ("summary", "price"), ())
        partial, verdict = partial_report(snapshot(), "report_support_incomplete", report, review)
        summary, findings = partial.markdown.split("## Findings", 1)
        self.assertIn(report.claims[0].text, summary)
        self.assertIn(f"[[source:{SOURCE}:L1-L1]]", summary)
        self.assertNotIn(report.claims[0].text, findings)
        self.assertEqual(partial.claims, report.claims)
        self.assertEqual(verdict.report_hash, partial.content_hash)
        self.assertEqual(verdict.source_hashes, review.source_hashes)

    def test_rejected_or_stale_summary_is_never_presented_as_a_finding(self) -> None:
        report = self.summary_report()
        for digest, accepted in (
            (report.content_hash, ("price",)),
            ("b" * 64, ("summary", "price")),
        ):
            with self.subTest(accepted=accepted):
                review = ResearchReview(digest, {SOURCE: "a" * 64}, accepted, ())
                partial, _ = partial_report(snapshot(), "report_support_incomplete", report, review)
                self.assertNotIn(report.claims[0].text, partial.markdown)

    def test_summary_label_alone_cannot_move_a_finding_into_the_summary(self) -> None:
        original = parse_report(report_json())
        report = replace(original, claims=(replace(original.claims[0], id="summary"),))
        review = ResearchReview(report.content_hash, {SOURCE: "a" * 64}, ("summary",), ())
        partial, _ = partial_report(snapshot(), "report_support_incomplete", report, review)
        self.assertNotIn(report.claims[0].text, partial.markdown.split("## Findings", 1)[0])
        self.assertIn(report.claims[0].text, partial.markdown.split("## Findings", 1)[1])

    def test_partial_findings_have_a_useful_summary_without_a_source_dump(self) -> None:
        report = parse_report(report_json())
        review = ResearchReview(report.content_hash, {SOURCE: "a" * 64}, ("price",), ())
        evidence: dict[str, object] = {
            "sources": [{"title": "Published pricing", "url": "https://example.com"}]
        }
        partial, _ = partial_report(
            snapshot(), "report_support_incomplete", report, review, evidence=evidence
        )
        self.assertIn("1 reviewed finding from 1 source", partial.markdown)
        self.assertNotIn("## Collected sources", partial.markdown)
        self.assertNotIn("report_support_incomplete", partial.markdown)
        self.assertEqual(partial.stop_reason, "report_support_incomplete")

    def test_zero_reviewed_findings_keep_sources_and_unfinished_questions(self) -> None:
        evidence: dict[str, object] = {
            "sources": [{"title": "Published pricing", "url": "https://example.com"}]
        }
        partial, review = partial_report(snapshot(), "time_limit", evidence=evidence)
        self.assertIn("before any findings could be verified", partial.markdown)
        self.assertIn("## Collected sources", partial.markdown)
        self.assertIn("Compare pricing", partial.markdown)
        self.assertIn("time limit", partial.markdown)
        self.assertNotIn("time_limit", partial.markdown)
        self.assertEqual(partial.stop_reason, "time_limit")
        self.assertEqual(review.accepted_claim_ids, ())

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
