import unittest
from dataclasses import replace

from app.application.research_reports import parse_report, parse_review, partial_report
from app.domain.research import ResearchClaim, ResearchReport, ResearchReview
from tests.test_research_reports import SOURCE, report_json, snapshot
from tests.test_research_v2_reports import price_review, v2_state


class PartialResearchOmissionTests(unittest.TestCase):
    def test_limitations_name_only_missing_reviewed_evidence_requirements(self):
        state = v2_state()
        original = parse_report(report_json())
        review = parse_review(price_review(), original, {SOURCE: "a" * 64}, snapshot=state)
        report, accepted = partial_report(state, "report_support_incomplete", original, review)
        limitations = report.markdown.split("## Limitations", 1)[1]
        self.assertIn("Billing terms", limitations)
        self.assertNotIn("Published price", limitations)
        self.assertEqual(report.claims, original.claims)
        self.assertEqual(accepted.source_hashes, review.source_hashes)
        self.assertEqual(accepted.report_hash, report.content_hash)

    def test_worker_findings_and_discarded_claims_cannot_hide_specific_omissions(self):
        state = v2_state()
        original = parse_report(report_json())
        reviewed = parse_review(price_review(), original, {SOURCE: "a" * 64}, snapshot=state)
        for review in (
            replace(reviewed, source_hashes={}),
            replace(reviewed, report_hash="b" * 64),
        ):
            with self.subTest(review=review):
                report, _ = partial_report(state, "time_limit", original, review)
                self.assertEqual(report.claims, ())
                limitations = report.markdown.split("## Limitations", 1)[1]
                self.assertIn("Published price", limitations)
                self.assertIn("Billing terms", limitations)

    def test_large_omission_labels_do_not_drop_reviewed_facts_near_report_size_limit(self):
        state = v2_state()
        state = replace(
            state,
            checkpoint={
                "subquestions": [
                    {
                        "id": f"q{question}",
                        "question": "A requested comparison " + "z" * 1900,
                        "evidence_requirements": [
                            {
                                "id": f"q{question}-r{requirement}",
                                "description": f"Missing area {question}/{requirement} "
                                + "y" * 1950,
                            }
                            for requirement in range(12)
                        ],
                    }
                    for question in range(12)
                ]
            },
        )
        reference = parse_report(report_json()).claims[0].citations
        claims = tuple(
            ResearchClaim(f"f{n}", f"Verified fact {n} " + "x" * 7800, reference) for n in range(8)
        )
        original = ResearchReport(
            "## Summary\n\nReviewed facts.\n\n## Findings\n\n"
            + "\n\n".join(claim.text for claim in claims)
            + "\n\n## Conflicts\n\nNone.\n\n## Limitations\n\nRemaining details.",
            claims,
            (),
            "time_limit",
        )
        review = ResearchReview(
            original.content_hash, {SOURCE: "a" * 64}, tuple(claim.id for claim in claims), (), ()
        )
        report, accepted = partial_report(state, "time_limit", original, review)
        self.assertEqual(report.claims, claims)
        self.assertEqual(accepted.accepted_claim_ids, review.accepted_claim_ids)
        self.assertLessEqual(len(report.markdown.encode()), 64 * 1024)
        limitations = report.markdown.split("## Limitations", 1)[1]
        self.assertIn("evidence areas omitted", limitations)
        self.assertLessEqual(len(limitations.encode()), 8 * 1024 + 200)

    def test_legacy_and_missing_requirement_metadata_keep_generic_question_disclosure(self):
        for state in (snapshot(), replace(v2_state(), checkpoint=snapshot().checkpoint)):
            with self.subTest(policy=state.policy_version):
                report, _ = partial_report(state, "time_limit")
                self.assertIn(
                    'Requested questions to revisit:\n- "Compare pricing"', report.markdown
                )
                self.assertNotIn("evidence areas", report.markdown)
