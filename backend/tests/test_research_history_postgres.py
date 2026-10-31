import json
import os
import unittest
from uuid import uuid4

from tests import test_research_postgres as database_tests


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchHistoryPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = database_tests.ResearchPostgresTests()
        self.db.setUp()
        self.addCleanup(self.db.tearDown)

    def summaries(
        self, *, offset: int = 0, limit: int = 100, newest: bool = False, turn: str | None = None
    ) -> list[dict]:
        turn_sql = "NULL" if turn is None else database_tests.literal(turn)
        result = self.db.sql(
            "SELECT trellis.read_session_run_summaries("
            + database_tests.literal(self.db.session_id)
            + f",{offset},{limit},{turn_sql},{str(newest).lower()});"
        )
        return json.loads(result.stdout)

    def test_research_mode_is_in_the_persisted_summary_contract(self) -> None:
        rows = self.summaries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run"]["id"], self.db.run_id)
        self.assertEqual(rows[0]["run"].get("run_mode"), "research")
        self.assertEqual(rows[0]["model_call_count"], 0)
        self.assertEqual(rows[0]["tool_call_count"], 0)
        self.assertEqual(rows[0]["total_tokens"], 0)
        self.assertTrue(rows[0]["usage_complete"])
        self.assertNotIn("lease_token", rows[0]["run"])

    def test_mixed_chat_history_preserves_modes_and_pagination(self) -> None:
        self.db.rpc(
            "transition_run_record",
            {
                "run_id": self.db.run_id,
                "next_status": "completed",
                "event_type": "run.completed",
                "data": {},
            },
            research=False,
        )
        model = json.loads(
            self.db.sql(
                "SELECT to_jsonb(model_id) FROM trellis.runs WHERE id="
                + database_tests.literal(self.db.run_id)
                + ";"
            ).stdout
        )
        chat_turn = str(uuid4())
        chat = self.db.rpc(
            "create_run",
            {
                "session_id": self.db.session_id,
                "turn_id": chat_turn,
                "client_request_id": str(uuid4()),
                "content": "Explain a database index",
                "model_id": model,
                "budget_preset": "conservative",
            },
            research=False,
        )["run"]
        first = self.summaries(limit=1)[0]["run"]
        second = self.summaries(offset=1, limit=1)[0]["run"]
        newest = self.summaries(limit=1, newest=True)[0]["run"]
        filtered = self.summaries(turn=chat_turn)
        self.assertEqual((first["id"], first.get("run_mode")), (self.db.run_id, "research"))
        self.assertEqual((second["id"], second.get("run_mode")), (chat["id"], "chat"))
        self.assertEqual(newest["id"], chat["id"])
        self.assertEqual([row["run"]["id"] for row in filtered], [chat["id"]])
        self.assertEqual(self.summaries(offset=2), [])

    def test_summary_scope_and_access_remain_owner_only(self) -> None:
        owner = self.db.owner
        self.db.owner = str(uuid4())
        try:
            self.assertEqual(self.summaries(), [])
        finally:
            self.db.owner = owner
        denied = self.db.sql(
            "SET ROLE anon; SELECT trellis.read_session_run_summaries("
            + database_tests.literal(self.db.session_id)
            + ");",
            authenticated=False,
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("permission denied", denied.stderr)


if __name__ == "__main__":
    unittest.main()
