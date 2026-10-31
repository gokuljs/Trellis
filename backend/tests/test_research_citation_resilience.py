import json
import unittest
from dataclasses import replace

from app.application.budgets import BudgetExceeded
from app.application.errors import ProviderError
from app.application.research_reports import parse_report, partial_report
from app.domain.research import ResearchReview
from tests import test_research_reports as report_fixtures
from tests.test_research_reports import SOURCE, report_json, snapshot
from tests.test_research_v2_reports import price_review, v2_state

MALFORMED_SOURCE = "00000000-0000-0000-0000-00000000000g"


def invalid_citation_report():
    value = json.loads(report_json())
    value["claims"].append(
        {
            "id": "bad-listing",
            "text": "Another listing is available.",
            "citations": [{"source_id": MALFORMED_SOURCE, "start_line": 1, "end_line": 1}],
        }
    )
    value["markdown"] = value["markdown"].replace(
        "## Conflicts",
        f"Another listing is available. [[source:{MALFORMED_SOURCE}:L1-L1]]\n\n## Conflicts",
    )
    return json.dumps(value)


def price_state():
    state = v2_state()
    question = dict(state.checkpoint["subquestions"][0])
    question["evidence_requirements"] = question["evidence_requirements"][:1]
    return replace(state, checkpoint={**state.checkpoint, "subquestions": [question]})


class ResearchCitationResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_receives_precise_claim_and_citation_diagnostic(self):
        fixture = report_fixtures.ResearchReportTests()
        published, purposes, _ = await fixture.run_report(
            [invalid_citation_report(), report_json(), price_review()], state=price_state()
        )
        self.assertEqual(purposes, ["draft", "repair", "recheck"])
        feedback = json.loads(fixture.prompts["repair"].rsplit("\n", 1)[-1])
        issue = feedback["validation_issues"][0]
        self.assertEqual(issue["claim_id"], "bad-listing")
        self.assertEqual(issue["citation_index"], 0)
        self.assertEqual(issue["source_id"], MALFORMED_SOURCE)
        self.assertIn("source_id", issue["issue"])
        self.assertEqual([claim.id for claim in published[0][0].claims], ["price"])

    async def test_invalid_repair_reviews_valid_subset_before_partial_publication(self):
        fixture = report_fixtures.ResearchReportTests()
        published, purposes, _ = await fixture.run_report(
            [invalid_citation_report(), invalid_citation_report(), price_review()],
            state=price_state(),
        )
        self.assertEqual(purposes, ["draft", "repair", "recheck"])
        prompt = fixture.prompts["recheck"]
        reviewed = json.loads(prompt[prompt.index('{"report":') :])["report"]
        self.assertEqual([claim["id"] for claim in reviewed["claims"]], ["price"])
        self.assertNotIn("Another listing is available.", reviewed["markdown"])
        report, review, partial = published[0]
        self.assertTrue(partial)
        self.assertEqual([claim.id for claim in report.claims], ["price"])
        self.assertEqual(review.report_hash, report.content_hash)
        self.assertEqual(review.source_hashes, {SOURCE: "a" * 64})
        self.assertNotIn(MALFORMED_SOURCE, report.markdown)

    async def test_reviewed_draft_and_invalid_repair_stay_within_four_calls(self):
        fixture = report_fixtures.ResearchReportTests()
        issues = json.loads(price_review())
        issues["issues"] = ["The report needs clearer limitations."]
        published, purposes, _ = await fixture.run_report(
            [report_json(), json.dumps(issues), invalid_citation_report(), price_review()],
            state=price_state(),
        )
        self.assertEqual(purposes, ["draft", "review", "repair", "recheck"])
        self.assertEqual([claim.id for claim in published[0][0].claims], ["price"])

    async def test_unreviewed_subset_never_publishes_when_recheck_budget_is_exhausted(self):
        fixture = report_fixtures.ResearchReportTests()
        published, purposes, _ = await fixture.run_report(
            [
                invalid_citation_report(),
                invalid_citation_report(),
                BudgetExceeded("model_call_limit"),
            ],
            state=price_state(),
        )
        self.assertEqual(purposes, ["draft", "repair", "recheck"])
        self.assertEqual(published[0][0].claims, ())

    async def test_subset_claim_rejected_by_semantic_review_never_publishes(self):
        fixture = report_fixtures.ResearchReportTests()
        rejected = json.dumps(
            {
                "accepted_claim_ids": [],
                "issues": ["The claim is unsupported."],
                "question_coverage": [],
            }
        )
        published, purposes, _ = await fixture.run_report(
            [invalid_citation_report(), invalid_citation_report(), rejected], state=price_state()
        )
        self.assertEqual(purposes, ["draft", "repair", "recheck"])
        self.assertEqual(published[0][0].claims, ())

    async def test_incomplete_repair_is_not_mined_for_subset_claims(self):
        from app.application.research import ResearchGeneration
        from tests.test_research_reports import CALL

        fixture = report_fixtures.ResearchReportTests()
        published, purposes, _ = await fixture.run_report(
            [
                invalid_citation_report(),
                ResearchGeneration(CALL, invalid_citation_report(), incomplete=True),
            ],
            state=price_state(),
        )
        self.assertEqual(purposes, ["draft", "repair"])
        self.assertEqual(published[0][0].claims, ())

    async def test_unknown_repair_outcome_does_not_start_subset_review(self):
        fixture = report_fixtures.ResearchReportTests()
        with self.assertRaises(ProviderError):
            await fixture.run_report(
                [invalid_citation_report(), ProviderError("model_outcome_unknown", "Lost")],
                state=price_state(),
            )
        self.assertEqual(set(fixture.prompts), {"draft", "repair"})

    async def test_legacy_invalid_repair_keeps_original_strict_call_sequence(self):
        fixture = report_fixtures.ResearchReportTests()
        published, purposes, _ = await fixture.run_report(
            [invalid_citation_report(), invalid_citation_report()]
        )
        self.assertEqual(purposes, ["draft", "repair"])
        self.assertEqual(published[0][0].claims, ())

    def test_partial_report_does_not_repeat_markers_already_inside_claim_text(self):
        marker = f"[[source:{SOURCE}:L1-L1]]"
        value = json.loads(report_json())
        value["claims"][0]["text"] += " " + marker
        report = parse_report(json.dumps(value))
        review = ResearchReview(report.content_hash, {SOURCE: "a" * 64}, ("price",), ())
        partial, _ = partial_report(snapshot(), "time_limit", report, review)
        self.assertEqual(partial.markdown.count(marker), 1)


if __name__ == "__main__":
    unittest.main()
