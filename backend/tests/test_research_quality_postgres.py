import hashlib
import json
import os
import unittest
from copy import deepcopy
from typing import cast
from uuid import uuid4

from tests import test_research_postgres as database_tests


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchQualityPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = database_tests.ResearchPostgresTests()
        self.db.setUp()
        self.addCleanup(self.db.tearDown)
        self.db.sql(
            "BEGIN; ALTER TABLE trellis.runs DISABLE TRIGGER preserve_research_profile;"
            "UPDATE trellis.runs SET research_policy_version='bounded-research-v2' WHERE id="
            + database_tests.literal(self.db.run_id)
            + "; ALTER TABLE trellis.runs ENABLE TRIGGER preserve_research_profile; COMMIT;",
            authenticated=False,
        )
        self.payload = self.db.publication()
        self.source_id = next(iter(self.payload["review"]["source_hashes"]))
        self.source_hash = self.payload["review"]["source_hashes"][self.source_id]
        self.payload["review"]["question_coverage"] = [
            {
                "question_id": "pricing",
                "requirement_ids": ["price"],
                "claim_ids": ["pricing"],
            }
        ]
        self.checkpoint: dict[str, object] = {
            "subquestions": [
                {
                    "id": "pricing",
                    "question": "What is the published price?",
                    "status": "addressed",
                    "evidence_requirements": [
                        {
                            "id": "price",
                            "description": "Published price",
                            "depth": "source_excerpt",
                        }
                    ],
                }
            ],
            "findings": [
                {
                    "id": "pricing",
                    "text": "価格は $10。",
                    "question_ids": ["pricing"],
                    "requirement_ids": ["price"],
                    "evidence_depth": "source_excerpt",
                    "citations": [{"source_id": self.source_id, "start_line": 1, "end_line": 2}],
                }
            ],
        }
        self.save_checkpoint()
        self.read_tool_id, self.read_message_id = self.seed_read()

    def save_checkpoint(self) -> None:
        self.db.sql(
            "UPDATE trellis.research_runs SET checkpoint="
            + database_tests.literal(json.dumps(self.checkpoint))
            + "::jsonb WHERE run_id="
            + database_tests.literal(self.db.run_id)
            + ";",
            authenticated=False,
        )

    def seed_read(self) -> tuple[str, str]:
        tool_id, message_id = (str(uuid4()) for _ in range(2))
        arguments = {"source_id": self.source_id, "start_line": 1, "line_count": 2}
        response = {
            "status": "success",
            "source_id": self.source_id,
            "content_hash": self.source_hash,
            "read_kind": "source_lines",
            "substantive": True,
            "presented_ranges": [{"start_line": 1, "end_line": 2}],
            "untrusted_lines": [
                {"line": 1, "text": "価格は $10。"},
                {"line": 2, "text": "The price applies to the standard plan."},
            ],
        }
        q = database_tests.literal
        self.db.sql(
            "INSERT INTO trellis.tool_calls(id,user_id,run_id,assistant_message_id,call_index,"
            "provider_call_id,name,arguments,status) VALUES("
            f"{q(tool_id)},{q(self.db.owner)},{q(self.db.run_id)},"
            "(SELECT id FROM trellis.run_messages WHERE model_call_id="
            + q(self.payload["model_call_id"])
            + "),1,"
            f"{q(str(uuid4()))},'web_source_read',{q(json.dumps(arguments))}::jsonb,'completed');"
            "INSERT INTO trellis.run_messages(id,user_id,run_id,ordinal,role,content,tool_call_id) "
            f"VALUES({q(message_id)},{q(self.db.owner)},{q(self.db.run_id)},3,'tool',"
            f"{q(json.dumps(response))},{q(tool_id)});",
            authenticated=False,
        )
        return tool_id, message_id

    def snapshot(self) -> dict:
        return json.loads(
            self.db.sql(
                "SELECT trellis.read_research(" + database_tests.literal(self.db.run_id) + ");"
            ).stdout
        )

    def rewrite_read(self, changes: dict) -> None:
        self.db.sql(
            "UPDATE trellis.run_messages SET content=(content::jsonb||"
            + database_tests.literal(json.dumps(changes))
            + "::jsonb)::text WHERE id="
            + database_tests.literal(self.read_message_id)
            + ";",
            authenticated=False,
        )

    def assert_no_publication(self) -> None:
        count = self.db.sql(
            "SELECT count(*) FROM trellis.run_assistant_outputs WHERE run_id="
            + database_tests.literal(self.db.run_id)
            + ";"
        ).stdout.strip()
        self.assertEqual(count, "0")

    def narrow_report(self, payload: dict) -> dict:
        narrowed = deepcopy(payload)
        narrowed["report"]["covered_question_ids"] = []
        digest = hashlib.sha256(
            json.dumps(
                narrowed["report"],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        narrowed["report_hash"] = digest
        narrowed["review"]["report_hash"] = digest
        return narrowed

    def test_complete_report_requires_reviewed_question_coverage(self) -> None:
        for coverage in (None, []):
            with self.subTest(coverage=coverage):
                rejected = self.db.rpc(
                    "publish",
                    {
                        **self.payload,
                        "review": {**self.payload["review"], "question_coverage": coverage},
                    },
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assert_no_publication()

    def test_supported_partial_report_can_publish_without_complete_question_coverage(self) -> None:
        questions = cast(list[dict[str, object]], self.checkpoint["subquestions"])
        requirements = cast(list[dict[str, object]], questions[0]["evidence_requirements"])
        requirements.append(
            {
                "id": "renewal",
                "description": "Renewal terms",
                "depth": "source_excerpt",
            }
        )
        self.save_checkpoint()
        denied = self.db.rpc("publish", self.payload, check=False)
        self.assertIn("research_claim_unsupported", denied.stderr)
        self.assert_no_publication()
        false_scope = self.db.rpc("publish", {**self.payload, "partial": True}, check=False)
        self.assertIn("research_claim_unsupported", false_scope.stderr)
        accepted = self.db.rpc("publish", {**self.narrow_report(self.payload), "partial": True})
        self.assertEqual(accepted["run"]["status"], "completed")
        saved = self.snapshot()
        self.assertEqual(saved["phase"], "partial")
        self.assertEqual(saved["reviewed_coverage"][0]["requirement_ids"], ["price"])

    def test_review_hash_and_revision_cannot_be_reused_for_changed_report(self) -> None:
        changed = deepcopy(self.payload)
        changed["report"]["markdown"] += "\nNew unsupported claim."
        rejected = self.db.rpc("publish", changed, check=False)
        self.assertIn("research_review_changed", rejected.stderr)
        self.assert_no_publication()

    def test_complete_coverage_cannot_point_to_an_accepted_claim_absent_from_report(self) -> None:
        changed = deepcopy(self.payload)
        changed["review"]["accepted_claim_ids"] = [
            *changed["review"]["accepted_claim_ids"],
            "invented",
        ]
        changed["review"]["question_coverage"][0]["claim_ids"] = ["invented"]
        rejected = self.db.rpc("publish", changed, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("research_review_changed", rejected.stderr)
        self.assert_no_publication()

    def test_v2_publication_is_exactly_once_and_replay_cannot_change_coverage(self) -> None:
        first = self.db.rpc("publish", self.payload)
        second = self.db.rpc("publish", self.payload)
        self.assertEqual(first, second)
        changed = deepcopy(self.payload)
        changed["review"]["question_coverage"] = []
        rejected = self.db.rpc("publish", changed, check=False)
        self.assertIn("research_operation_conflict", rejected.stderr)
        outputs = self.db.sql(
            "SELECT count(*) FROM trellis.run_assistant_outputs WHERE run_id="
            + database_tests.literal(self.db.run_id)
            + ";"
        ).stdout.strip()
        self.assertEqual(outputs, "1")

    def test_snapshot_requires_actual_saved_line_content_and_matching_source_hash(self) -> None:
        first = self.snapshot()
        self.assertEqual(len(first["source_reads"]), 1)
        self.assertEqual(first["source_reads"][0]["source_id"], self.source_id)
        self.rewrite_read({"content_hash": "0" * 64})
        self.assertEqual(self.snapshot()["source_reads"], [])

    def test_unobserved_or_tampered_evidence_cannot_establish_complete_coverage(self) -> None:
        self.rewrite_read({"content_hash": "0" * 64})
        self.assertEqual(self.snapshot()["source_reads"], [])
        rejected = self.db.rpc("publish", self.payload, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("research_claim_unsupported", rejected.stderr)
        self.assert_no_publication()
        self.rewrite_read(
            {
                "content_hash": self.source_hash,
                "untrusted_lines": [
                    {"line": 1, "text": "Invented body under the real source hash"},
                    {"line": 2, "text": "The price applies to the standard plan."},
                ],
            }
        )
        self.assertEqual(self.snapshot()["source_reads"], [])

    def test_snapshot_rejects_discovery_metadata_and_incomplete_claimed_ranges(self) -> None:
        self.rewrite_read({"read_kind": "outline", "substantive": False})
        self.assertEqual(self.snapshot()["source_reads"], [])
        self.rewrite_read(
            {
                "read_kind": "source_lines",
                "substantive": True,
                "presented_ranges": [{"start_line": 1, "end_line": 1}],
            }
        )
        self.assertEqual(self.snapshot()["source_reads"], [])

    def test_legacy_publication_and_replay_keep_their_original_wire_contract(self) -> None:
        legacy = database_tests.ResearchPostgresTests()
        legacy.setUp()
        try:
            payload = legacy.publication()
            payload["review"].pop("question_coverage", None)
            first = legacy.rpc("publish", payload)
            self.assertEqual(first, legacy.rpc("publish", payload))
            self.assertEqual(first["run"]["status"], "completed")
        finally:
            legacy.tearDown()
