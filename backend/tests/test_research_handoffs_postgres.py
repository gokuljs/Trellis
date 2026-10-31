import concurrent.futures
import json
import os
import unittest
from uuid import uuid4

from tests import test_research_postgres as database_tests


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchHandoffPostgresTests(unittest.TestCase):
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

    def hold(self, task: str, tokens: int = 70_000, cost: float = 0.1, *, check: bool = True):
        return self.db.rpc(
            "set_handoff",
            {
                "run_id": self.db.run_id,
                "generation": 0,
                "task_id": task,
                "reserved_tokens": tokens,
                "reserved_cost": cost,
            },
            check=check,
        )

    def count(self, table: str) -> int:
        return int(
            self.db.sql(
                f"SELECT count(*) FROM trellis.{table} WHERE run_id="
                + database_tests.literal(self.db.run_id)
                + ";",
                authenticated=False,
            ).stdout
        )

    def test_parallel_collection_cannot_spend_another_workers_handoff(self) -> None:
        for task in self.db.tasks:
            self.hold(task)
        self.assertEqual(self.count("model_calls"), 0)
        payloads = [self.db.reservation(task, tokens=50_000) for task in self.db.tasks]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda payload: self.db.rpc("reserve_call", payload, check=False), payloads
                )
            )
        self.assertEqual(sum(result.returncode == 0 for result in results), 1)
        denied = next(result for result in results if result.returncode)
        self.assertIn("research_finalization_reserve", denied.stderr)
        task = self.db.tasks[
            next(index for index, result in enumerate(results) if result.returncode)
        ]
        handoff = {**self.db.reservation(task, tokens=70_000), "handoff": True}
        first = self.db.rpc("reserve_call", handoff)
        replayed = self.db.rpc("reserve_call", handoff)
        self.assertEqual(first, replayed)
        self.assertEqual(self.count("research_handoff_holds"), 1)
        self.assertEqual(self.count("model_calls"), 2)

    def test_holds_are_idempotent_and_resize_failure_preserves_the_original_hold(self) -> None:
        first = self.hold(self.db.tasks[0])
        second = self.hold(self.db.tasks[0])
        self.assertEqual(first, second)
        denied = self.hold(self.db.tasks[0], tokens=226_000, check=False)
        self.assertNotEqual(denied.returncode, 0)
        state = self.db.rpc("budget_state", {"run_id": self.db.run_id, "generation": 0})
        self.assertEqual(state["held"]["tokens"], 70_000)
        self.assertEqual(state["held"]["model_calls"], 1)
        self.assertEqual(state["used"]["model_calls"], 0)
        self.assertEqual(state["finalization_reserve"]["tokens"], 75_000)

    def test_root_call_allowance_includes_held_checkpoint_calls(self) -> None:
        self.db.sql(
            "UPDATE trellis.runs SET max_model_calls=6 WHERE id="
            + database_tests.literal(self.db.run_id)
            + ";",
            authenticated=False,
        )
        for task in self.db.tasks:
            self.hold(task, tokens=1000)
        denied = self.db.rpc("reserve_call", self.db.reservation(self.db.tasks[0]), check=False)
        self.assertIn("research_finalization_reserve", denied.stderr)
        self.db.rpc("reserve_call", {**self.db.reservation(self.db.tasks[0]), "handoff": True})
        self.assertEqual(self.count("model_calls"), 1)
        self.assertEqual(self.count("research_handoff_holds"), 1)

    def test_cancellation_releases_unused_holds_and_wins_dispatch(self) -> None:
        self.hold(self.db.tasks[0])
        self.db.sql(
            "UPDATE trellis.runs SET cancel_requested_at=clock_timestamp() WHERE id="
            + database_tests.literal(self.db.run_id)
            + ";",
            authenticated=False,
        )
        self.assertEqual(self.count("research_handoff_holds"), 0)
        denied = self.db.rpc(
            "reserve_call", {**self.db.reservation(self.db.tasks[0]), "handoff": True}, check=False
        )
        self.assertIn("research_scope_invalid", denied.stderr)

    def test_generation_takeover_releases_holds_and_rejects_the_stale_worker(self) -> None:
        self.hold(self.db.tasks[0])
        self.db.sql(
            "UPDATE trellis.runs SET generation=generation+1 WHERE id="
            + database_tests.literal(self.db.run_id)
            + ";",
            authenticated=False,
        )
        self.assertEqual(self.count("research_handoff_holds"), 0)
        denied = self.hold(self.db.tasks[0], check=False)
        self.assertIn("research_scope_invalid", denied.stderr)

    def test_unknown_paid_outcome_releases_holds_and_blocks_all_model_work(self) -> None:
        for task in self.db.tasks:
            self.hold(task)
        call = self.db.rpc("reserve_call", self.db.reservation(self.db.tasks[0]))["model_call"]
        self.db.rpc(
            "mark_unknown", {"run_id": self.db.run_id, "generation": 0, "call_id": call["id"]}
        )
        self.assertEqual(self.count("research_handoff_holds"), 0)
        denied = self.db.rpc(
            "reserve_call", {**self.db.reservation(self.db.tasks[1]), "handoff": True}, check=False
        )
        self.assertIn("research_usage_unknown", denied.stderr)

    def test_task_exit_and_explicit_release_are_safe_to_repeat(self) -> None:
        self.hold(self.db.tasks[0])
        payload = {"run_id": self.db.run_id, "generation": 0, "task_id": self.db.tasks[0]}
        self.db.rpc("release_handoff", payload)
        self.db.rpc("release_handoff", payload)
        self.assertEqual(self.count("research_handoff_holds"), 0)
        self.hold(self.db.tasks[0])
        self.db.sql(
            "UPDATE trellis.research_tasks SET status='completed' WHERE id="
            + database_tests.literal(self.db.tasks[0])
            + ";",
            authenticated=False,
        )
        self.assertEqual(self.count("research_handoff_holds"), 0)

    def test_holds_are_scoped_to_the_owned_task_and_collection_deadline(self) -> None:
        denied = self.hold(str(uuid4()), check=False)
        self.assertIn("research_scope_invalid", denied.stderr)
        self.db.sql(
            "UPDATE trellis.runs SET deadline_at=clock_timestamp()+interval '149 seconds' WHERE id="
            + database_tests.literal(self.db.run_id)
            + ";",
            authenticated=False,
        )
        denied = self.hold(self.db.tasks[0], check=False)
        self.assertIn("research_finalization_reserve", denied.stderr)
        self.assertEqual(self.count("model_calls"), 0)

    def test_cost_holds_protect_the_report_allowance(self) -> None:
        self.hold(self.db.tasks[0], tokens=1000, cost=4)
        denied = self.hold(self.db.tasks[1], tokens=1000, cost=0.6, check=False)
        self.assertIn("research_finalization_reserve", denied.stderr)
        state = self.db.rpc("budget_state", {"run_id": self.db.run_id, "generation": 0})
        self.assertEqual(state["held"]["estimated_cost"], 4)
        self.assertEqual(state["collection_remaining"]["estimated_cost"], 0.5)
        self.assertEqual(
            json.loads(json.dumps(state))["finalization_reserve"]["estimated_cost"], 1.5
        )

    def test_saved_source_tools_have_recoverable_dispatch_and_valid_web_scope(self) -> None:
        for name in ("web_source_read", "web_source_find", "web_source_outline"):
            with self.subTest(name=name):
                committed = self.db.completed_task(tool_call=True, tool_name=name)
                tool = committed["tool_calls"][0]
                self.assertEqual(tool["recovery_policy"], "checkpointed")
                dispatch_id = str(uuid4())
                dispatched = self.db.rpc(
                    "begin_tool_execution",
                    {
                        "run_id": self.db.run_id,
                        "tool_call_id": tool["id"],
                        "dispatch_id": dispatch_id,
                        "expected_attempt": 0,
                        "recovery_policy": "checkpointed",
                        "input_snapshot": {"arguments": tool["arguments"]},
                    },
                    research=False,
                )
                self.assertEqual(dispatched["tool_call"]["status"], "running")
                payload = {
                    "user_id": self.db.owner,
                    "session_id": self.db.session_id,
                    "run_id": self.db.run_id,
                    "tool_call_id": tool["id"],
                    "dispatch_id": dispatch_id,
                    "dispatch_attempt": 1,
                    "generation": 0,
                }
                scoped = self.db.sql(
                    "SELECT trellis.web_mutate('open_run',"
                    + database_tests.literal(json.dumps(payload))
                    + "::jsonb,"
                    + database_tests.literal(self.db.capability)
                    + ");"
                )
                self.assertEqual(json.loads(scoped.stdout)["usage"]["network_requests"], 0)


if __name__ == "__main__":
    unittest.main()
