import hashlib
import json
import os
import unittest
from uuid import uuid4

import test_research_postgres as database_tests


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchSettlementPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = database_tests.ResearchPostgresTests()
        self.database.setUp()
        self.addCleanup(self.database.tearDown)

    def generation(self, tool_name: str) -> dict:
        db = self.database
        admitted = db.rpc("reserve_call", db.reservation(db.tasks[0]))
        call_id = admitted["model_call"]["id"]
        db.rpc(
            "update_model_call", {"call_id": call_id, "next_status": "streaming"}, research=False
        )
        tool_calls = [{"id": str(uuid4()), "name": tool_name, "arguments": {"findings": []}}]
        response = {
            "content": "Private checkpoint",
            "tool_calls": tool_calls,
            "continuation_items": [],
            "provider_response_id": "test-response",
            "finish_reason": "tool_calls",
            "finish_kind": "tool",
        }
        return {
            "run_id": db.run_id,
            "generation": 0,
            "task_id": db.tasks[0],
            "call_id": call_id,
            "completion": {
                "response_snapshot": response,
                "provider_response_id": "test-response",
                "finish_reason": "tool_calls",
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "cached_read_tokens": 0,
                "cache_creation_tokens": 0,
                "estimated_cost": 0.001,
                "accounted_total_tokens": 120,
            },
            "assistant_message": {
                "role": "assistant",
                "content": response["content"],
                "tool_calls": tool_calls,
                "continuation_items": [],
                "tool_call_id": None,
            },
            "terminal_result": None,
        }

    def test_checkpoint_control_dispatches_and_result_replays(self) -> None:
        db = self.database
        generation = db.rpc("commit_generation", self.generation("research_checkpoint"))
        tool = generation["tool_calls"][0]
        self.assertEqual(tool["recovery_policy"], "checkpointed")
        dispatched = db.rpc(
            "begin_tool_execution",
            {
                "run_id": db.run_id,
                "tool_call_id": tool["id"],
                "dispatch_id": str(uuid4()),
                "expected_attempt": 0,
                "recovery_policy": "checkpointed",
                "input_snapshot": None,
            },
            research=False,
        )
        self.assertEqual(dispatched["tool_call"]["status"], "running")
        content = '{"saved":true}'
        result = {
            "run_id": db.run_id,
            "tool_call_id": tool["id"],
            "content": content,
            "status": "completed",
            "result_source": "executor",
            "result_snapshot": {
                "source": "checkpointed_web",
                "dispatch_id": dispatched["tool_call"]["dispatch_id"],
                "generation": 0,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            },
        }
        first = db.rpc("record_tool_result", result, research=False)
        self.assertEqual(first, db.rpc("record_tool_result", result, research=False))
        self.assertEqual(first["tool_call"]["status"], "completed")

    def test_rejected_paid_response_keeps_known_usage_without_tool_effects(self) -> None:
        db = self.database
        payload = self.generation("unoffered_tool")
        payload.pop("assistant_message")
        payload.pop("terminal_result")
        payload["error_code"] = "unknown_tool"
        first = db.rpc("reject_generation", payload)
        self.assertEqual(first, db.rpc("reject_generation", payload))
        self.assertEqual(first["tool_calls"], [])
        self.assertEqual(first["model_call"]["status"], "completed")
        self.assertEqual(first["model_call"]["error_code"], "unknown_tool")
        self.assertEqual(first["model_call"]["accounted_total_tokens"], 120)
        self.assertEqual(first["model_call"]["estimated_cost"], 0.001)
        quoted = database_tests.literal(db.run_id)
        state = json.loads(
            db.sql(
                "SELECT jsonb_build_object('tools',(SELECT count(*) FROM trellis.tool_calls "
                f"WHERE run_id={quoted}),'settled',"
                "(SELECT actual_tokens FROM trellis.research_reservations "
                f"WHERE run_id={quoted}),'task',(SELECT status FROM trellis.research_tasks "
                f"WHERE id={database_tests.literal(db.tasks[0])}));",
                authenticated=False,
            ).stdout
        )
        self.assertEqual(state, {"tools": 0, "settled": 120, "task": "failed"})

    def test_worker_cannot_delegate_even_through_direct_generation_commit(self) -> None:
        rejected = self.database.rpc(
            "commit_generation", self.generation("delegate_research"), check=False
        )
        self.assertIn("research_scope_invalid", rejected.stderr)

    def test_budget_stop_checkpoint_marks_task_terminal(self) -> None:
        db = self.database
        saved = db.rpc(
            "checkpoint",
            {
                "run_id": db.run_id,
                "generation": 0,
                "task_id": db.tasks[0],
                "expected_revision": 0,
                "operation_id": str(uuid4()),
                "checkpoint": {"stop_reason": "research_finalization_reserve"},
            },
        )
        self.assertEqual(saved["status"], "failed")


if __name__ == "__main__":
    unittest.main()
