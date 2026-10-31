import json
import unittest
from dataclasses import replace
from uuid import uuid4

from app.application.budgets import BudgetExceeded
from app.application.research_reports import parse_report, parse_review, partial_report
from app.domain.research import RESEARCH_POLICY_V2, ResearchTask, ResearchTaskStatus
from tests import test_research_reports as report_fixtures
from tests.test_research_reports import (
    SOURCE,
    report_json,
    snapshot,
)


def v2_state():
    return replace(
        snapshot(),
        policy_version=RESEARCH_POLICY_V2,
        checkpoint={
            "brief": {"scope": "Compare pricing and billing terms", "source_restrictions": []},
            "subquestions": [
                {
                    "id": "pricing",
                    "question": "Compare pricing and billing terms",
                    "evidence_requirements": [
                        {
                            "id": "price",
                            "description": "Published price",
                            "depth": "source_excerpt",
                        },
                        {"id": "terms", "description": "Billing terms", "depth": "source_excerpt"},
                    ],
                }
            ],
            "findings": [
                {
                    "id": "price-finding",
                    "text": "Product A costs $10.",
                    "question_ids": ["pricing"],
                    "requirement_ids": ["price"],
                    "evidence_depth": "source_excerpt",
                    "citations": [{"source_id": SOURCE, "start_line": 1, "end_line": 1}],
                }
            ],
        },
        source_reads=(
            {
                "source_id": SOURCE,
                "content_hash": "a" * 64,
                "presented_ranges": [{"start_line": 1, "end_line": 1}],
            },
        ),
    )


def price_review() -> str:
    return json.dumps(
        {
            "accepted_claim_ids": ["price"],
            "issues": [],
            "question_coverage": [
                {
                    "question_id": "pricing",
                    "requirement_ids": ["price"],
                    "claim_ids": ["price"],
                }
            ],
        }
    )


class V2ReviewBindingTests(unittest.TestCase):
    def test_unrelated_accepted_claim_cannot_borrow_another_claims_requirement_support(self):
        other_source = str(uuid4())
        report = parse_report(
            json.dumps(
                {
                    "markdown": (
                        "## Summary\n\nComparison.\n\n## Findings\n\n"
                        f"Product A costs $10. [[source:{SOURCE}:L1-L1]]\n\n"
                        f"The publisher is Example. [[source:{other_source}:L10-L10]]\n\n"
                        "## Conflicts\n\nNo verified conflict.\n\n"
                        "## Limitations\n\nOnly public sources."
                    ),
                    "claims": [
                        {
                            "id": "price",
                            "text": "Product A costs $10.",
                            "citations": [
                                {
                                    "source_id": SOURCE,
                                    "start_line": 1,
                                    "end_line": 1,
                                }
                            ],
                        },
                        {
                            "id": "publisher",
                            "text": "The publisher is Example.",
                            "citations": [
                                {
                                    "source_id": other_source,
                                    "start_line": 10,
                                    "end_line": 10,
                                }
                            ],
                        },
                    ],
                    "covered_question_ids": ["pricing"],
                    "stop_reason": "coverage_sufficient",
                }
            )
        )
        state = v2_state()
        question = dict(state.checkpoint["subquestions"][0])
        question["evidence_requirements"] = question["evidence_requirements"][:1]
        state = replace(state, checkpoint={**state.checkpoint, "subquestions": [question]})
        result = parse_review(
            json.dumps(
                {
                    "accepted_claim_ids": ["price", "publisher"],
                    "issues": [],
                    "question_coverage": [
                        {
                            "question_id": "pricing",
                            "requirement_ids": ["price"],
                            "claim_ids": ["publisher"],
                        }
                    ],
                }
            ),
            report,
            {SOURCE: "a" * 64, other_source: "b" * 64},
            snapshot=state,
        )
        self.assertEqual(result.accepted_claim_ids, ("price", "publisher"))
        self.assertEqual(result.question_coverage, ())
        self.assertTrue(result.issues)

    def test_missing_question_evidence_preserves_accepted_claims_for_partial_results(self):
        report = parse_report(report_json())
        result = parse_review(price_review(), report, {SOURCE: "a" * 64}, snapshot=v2_state())
        self.assertEqual(result.accepted_claim_ids, ("price",))
        assert result.question_coverage is not None
        self.assertEqual(result.question_coverage[0].requirement_ids, ("price",))
        self.assertTrue(result.issues)
        self.assertEqual(result.report_hash, report.content_hash)

    def test_v2_review_without_coverage_is_invalid_but_legacy_review_remains_valid(self):
        text = json.dumps({"accepted_claim_ids": ["price"], "issues": []})
        report = parse_report(report_json())
        self.assertIsNone(parse_review(text, report, {SOURCE: "a" * 64}).question_coverage)
        with self.assertRaises(ValueError):
            parse_review(text, report, {SOURCE: "a" * 64}, snapshot=v2_state())

    def test_partial_fallback_preserves_answered_questions_without_hiding_remaining_gaps(self):
        state = v2_state()
        price = dict(state.checkpoint["subquestions"][0])
        price["question"] = "Which price is verified?"
        price["evidence_requirements"] = price["evidence_requirements"][:1]
        renewal = {
            "id": "renewal",
            "question": "What renewal clauses remain unknown?",
            "evidence_requirements": [
                {
                    "id": "renewal-clauses",
                    "description": "Renewal clauses",
                    "depth": "source_excerpt",
                }
            ],
        }
        state = replace(state, checkpoint={**state.checkpoint, "subquestions": [price, renewal]})
        report = parse_report(report_json())
        review = parse_review(price_review(), report, {SOURCE: "a" * 64}, snapshot=state)
        result, accepted = partial_report(state, "token_limit", report, review)
        self.assertEqual(result.covered_question_ids, ("pricing",))
        assert accepted.question_coverage is not None
        self.assertEqual(accepted.question_coverage[0].question_id, "pricing")
        self.assertEqual(accepted.report_hash, result.content_hash)
        self.assertIn("What renewal clauses remain unknown?", result.markdown)
        self.assertNotIn("Which price is verified?", result.markdown)


class V2ReportFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_gaps_and_conflicts_reach_both_writer_and_reviewer(self):
        state = v2_state()
        worker = ResearchTask(
            str(uuid4()),
            state.run_id,
            "Check billing conditions",
            "worker",
            1,
            ResearchTaskStatus.FAILED,
            1,
            {
                "gaps": [
                    {
                        "id": "renewal-page",
                        "text": "The renewal page was unavailable",
                        "question_ids": ["pricing"],
                        "status": "unavailable",
                        "citations": [],
                    }
                ],
                "conflicts": [
                    {
                        "id": "currency",
                        "text": "A USD versus local-currency discrepancy remains",
                        "question_ids": ["pricing"],
                        "status": "open",
                        "citations": [],
                    }
                ],
            },
        )
        state = replace(state, tasks=(worker,))
        fixture = report_fixtures.ResearchReportTests()
        narrowed = json.loads(report_json())
        narrowed["covered_question_ids"] = []
        await fixture.run_report([json.dumps(narrowed), price_review()], state=state)
        for purpose in ("draft", "review"):
            with self.subTest(purpose=purpose):
                self.assertIn("The renewal page was unavailable", fixture.prompts[purpose])
                self.assertIn(
                    "A USD versus local-currency discrepancy remains", fixture.prompts[purpose]
                )

    async def test_one_repair_then_recheck_publishes_supported_partial_scope(self):
        fixture = report_fixtures.ResearchReportTests()
        narrowed = json.loads(report_json())
        narrowed["covered_question_ids"] = []
        published, purposes, _ = await fixture.run_report(
            [
                report_json(),
                price_review(),
                json.dumps(narrowed),
                price_review(),
            ],
            state=v2_state(),
        )
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        self.assertEqual(len(published), 1)
        report, review, partial = published[0]
        self.assertTrue(partial)
        self.assertEqual([claim.id for claim in report.claims], ["price"])
        self.assertEqual(review.report_hash, report.content_hash)
        self.assertEqual(review.question_coverage[0].requirement_ids, ("price",))

    async def test_exhausted_repair_keeps_reviewed_findings_and_rebinds_partial_hash(self):
        fixture = report_fixtures.ResearchReportTests()
        published, purposes, _ = await fixture.run_report(
            [
                report_json(),
                price_review(),
                BudgetExceeded("model_call_limit"),
            ],
            state=v2_state(),
        )
        self.assertEqual(purposes, ["draft", "review", "repair"])
        report, review, partial = published[0]
        self.assertTrue(partial)
        self.assertIn("Product A costs $10.", report.markdown)
        self.assertNotIn("No reviewed findings", report.markdown)
        self.assertEqual(review.accepted_claim_ids, ("price",))
        self.assertEqual(review.report_hash, report.content_hash)
        self.assertNotEqual(review.report_hash, parse_report(report_json()).content_hash)
