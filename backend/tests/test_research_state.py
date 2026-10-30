import unittest
from dataclasses import replace
from uuid import uuid4

from app.domain.research import (
    MAX_RESEARCH_CHECKPOINT_BYTES,
    MAX_RESEARCH_REPORT_BYTES,
    ResearchClaim,
    ResearchReport,
    ResearchReservation,
    ResearchReview,
    ResearchSnapshot,
    ResearchTaskInput,
    research_limits,
    research_object,
)
from app.domain.web import CitationReference


class ResearchStateTests(unittest.TestCase):
    def test_checkpoint_limits_count_encoded_bytes(self) -> None:
        with self.assertRaises(ValueError):
            research_object({"findings": "界" * (MAX_RESEARCH_CHECKPOINT_BYTES // 3)})

    def test_checkpoint_rejects_nonfinite_values(self) -> None:
        with self.assertRaises(ValueError):
            research_object({"confidence": float("nan")})

    def test_checkpoint_rejects_nested_null_characters(self) -> None:
        with self.assertRaises(ValueError):
            research_object({"findings": [{"claim": "bad\0text"}]})

    def test_checkpoint_copies_nested_input(self) -> None:
        original: dict[str, object] = {"findings": ["first"]}
        saved = research_object(original)
        original["findings"] = ["replacement"]
        self.assertEqual(saved, {"findings": ["first"]})

    def test_worker_requires_real_collection_round(self) -> None:
        with self.assertRaises(ValueError):
            ResearchTaskInput(str(uuid4()), "Compare pricing", round_index=0)

    def test_limits_keep_shared_time_cost_and_token_caps(self) -> None:
        standard = research_limits("conservative")
        extended = research_limits("longer")
        self.assertEqual(
            (standard.seconds, standard.model_cost_usd, standard.total_tokens), (600, 2.0, 100_000)
        )
        self.assertEqual(
            (extended.seconds, extended.model_cost_usd, extended.total_tokens), (1200, 5.0, 200_000)
        )
        self.assertEqual((standard.worker_tasks, standard.collection_rounds), (4, 2))
        self.assertEqual((extended.worker_tasks, extended.collection_rounds), (8, 4))

    def test_snapshot_rejects_another_runs_task(self) -> None:
        run_id = str(uuid4())
        with self.assertRaises(ValueError):
            ResearchSnapshot.from_dict(
                {
                    "run_id": run_id,
                    "user_id": str(uuid4()),
                    "session_id": str(uuid4()),
                    "phase": "researching",
                    "objective": "Compare products",
                    "revision": 1,
                    "checkpoint": {},
                    "pending_clarification": None,
                    "remaining_seconds": 400,
                    "tasks": [
                        {
                            "id": str(uuid4()),
                            "run_id": str(uuid4()),
                            "objective": "Compare prices",
                            "role": "worker",
                            "round_index": 1,
                            "status": "running",
                            "revision": 0,
                            "checkpoint": {},
                        }
                    ],
                }
            )

    def test_report_hash_changes_with_citation_ranges(self) -> None:
        source_id = str(uuid4())
        report = ResearchReport(
            "The plan costs $10.",
            (ResearchClaim("price", "The plan costs $10.", (CitationReference(source_id, 1, 2),)),),
            ("pricing",),
            "coverage_sufficient",
        )
        changed = replace(
            report,
            claims=(replace(report.claims[0], citations=(CitationReference(source_id, 2, 3),)),),
        )
        self.assertNotEqual(report.content_hash, changed.content_hash)

    def test_report_rejects_claim_absent_from_text(self) -> None:
        with self.assertRaises(ValueError):
            ResearchReport(
                "No price was found.",
                (
                    ResearchClaim(
                        "price", "The plan costs $10.", (CitationReference(str(uuid4()), 1, 2),)
                    ),
                ),
                (),
                "evidence_unavailable",
            )

    def test_report_limits_count_encoded_bytes(self) -> None:
        with self.assertRaises(ValueError):
            ResearchReport("界" * (MAX_RESEARCH_REPORT_BYTES // 3 + 1), (), (), "budget_limit")

    def test_review_rejects_malformed_source_hash(self) -> None:
        with self.assertRaises(ValueError):
            ResearchReview("0" * 64, {str(uuid4()): "not-a-hash"}, (), ())

    def test_reservation_rejects_unknown_status(self) -> None:
        with self.assertRaises(ValueError):
            ResearchReservation.from_dict(
                {
                    "id": str(uuid4()),
                    "run_id": str(uuid4()),
                    "task_id": str(uuid4()),
                    "model_call_id": str(uuid4()),
                    "reserved_tokens": 500,
                    "reserved_cost": 0.2,
                    "finalization": False,
                    "status": "refunded",
                }
            )


if __name__ == "__main__":
    unittest.main()
