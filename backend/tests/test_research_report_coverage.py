import json
import unittest
from dataclasses import replace

from app.application.research_report_coverage import review_question_coverage
from app.application.research_reports import parse_report
from app.domain.research import RESEARCH_POLICY_V2
from tests.test_research_reports import SOURCE, report_json, snapshot


def research_state():
    return replace(
        snapshot(),
        policy_version=RESEARCH_POLICY_V2,
        checkpoint={
            "subquestions": [
                {
                    "id": "pricing",
                    "question": "Compare pricing and terms",
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
                    "id": "price",
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


class ResearchReportCoverageTests(unittest.TestCase):
    def test_writer_notes_keep_whole_contracts_within_their_byte_limit(self):
        from app.application.research_reports import _writer_notes

        state = research_state()
        question = dict(state.checkpoint["subquestions"][0])
        question["evidence_requirements"] = [
            {"id": str(n), "description": "x" * 2000, "depth": "full_text"} for n in range(12)
        ]
        state = replace(
            state, checkpoint={"subquestions": [{**question, "id": f"q{n}"} for n in range(10)]}
        )
        value = _writer_notes(state, {"admitted_findings": []})
        self.assertLess(len(json.dumps(value, ensure_ascii=False).encode()), 34 * 1024)
        self.assertTrue(value["omitted_question_ids"])

    def review(self, state=None, *, requirements=None, claims=None):
        return review_question_coverage(
            [
                {
                    "question_id": "pricing",
                    "requirement_ids": requirements or ["price"],
                    "claim_ids": claims or ["price"],
                }
            ],
            parse_report(report_json()),
            state or research_state(),
            ("price",),
            {SOURCE: "a" * 64},
        )

    def test_supported_claim_does_not_establish_complete_question_coverage(self):
        coverage, issues = self.review()
        self.assertEqual(coverage[0].requirement_ids, ("price",))
        self.assertTrue(issues)

    def test_unknown_requirement_and_unaccepted_claim_are_rejected(self):
        for changes in ({"requirements": ["invented"]}, {"claims": ["invented"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.review(**changes)

    def test_model_declared_depth_cannot_replace_observed_evidence(self):
        state = research_state()
        question = dict(state.checkpoint["subquestions"][0])
        question["evidence_requirements"] = [
            {
                "id": "price",
                "description": "Read full method",
                "depth": "full_text",
            }
        ]
        state = replace(state, checkpoint={**state.checkpoint, "subquestions": [question]})
        coverage, issues = self.review(state)
        self.assertEqual(coverage, ())
        self.assertTrue(issues)

    def test_changed_source_hash_cannot_establish_coverage(self):
        state = replace(
            research_state(),
            source_reads=(
                {
                    "source_id": SOURCE,
                    "content_hash": "b" * 64,
                    "presented_ranges": [{"start_line": 1, "end_line": 1}],
                },
            ),
        )
        coverage, issues = self.review(state)
        self.assertEqual(coverage, ())
        self.assertTrue(issues)

    def test_complete_question_requires_all_evidence_facets(self):
        state = research_state()
        question = dict(state.checkpoint["subquestions"][0])
        question["evidence_requirements"] = question["evidence_requirements"][:1]
        state = replace(state, checkpoint={**state.checkpoint, "subquestions": [question]})
        coverage, issues = self.review(state)
        self.assertEqual(coverage[0].question_id, "pricing")
        self.assertEqual(issues, ())

    def test_legacy_review_wire_shape_does_not_gain_new_fields(self):
        from dataclasses import asdict

        from app.domain.research import ResearchReview, research_review_payload

        review = ResearchReview("a" * 64, {}, (), ())
        self.assertEqual(
            set(research_review_payload(review)),
            {
                "report_hash",
                "source_hashes",
                "accepted_claim_ids",
                "issues",
            },
        )
        self.assertEqual(json.loads(json.dumps(research_review_payload(review)))["issues"], [])
        self.assertIsNone(asdict(review)["question_coverage"])
