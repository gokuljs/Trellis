import json
import os
import unittest
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource
from tests import test_research_postgres as database_tests
from tests.test_research_agent_loop import MemoryResearch, make_run


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchBudgetPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = database_tests.ResearchPostgresTests()
        self.db.budget_preset = "longer"
        self.db.setUp()
        self.addCleanup(self.db.tearDown)

    def create_run(self, mode: str, preset: str) -> dict:
        session = self.db.rpc("create_session", {}, research=False)
        model = json.loads(
            self.db.sql(
                "SELECT to_jsonb(model_id) FROM trellis.runs WHERE id="
                + database_tests.literal(self.db.run_id)
                + ";"
            ).stdout
        )
        return self.db.rpc(
            "create_run",
            {
                "session_id": session["id"],
                "turn_id": str(uuid4()),
                "client_request_id": str(uuid4()),
                "content": "Compare products",
                "model_id": model,
                "budget_preset": preset,
                "run_mode": mode,
            },
            research=False,
        )["run"]

    def test_new_research_runs_triple_model_allowances(self) -> None:
        for preset, expected in (
            ("conservative", (72, 192, 300_000, 6, 600)),
            ("longer", (120, 384, 600_000, 15, 1200)),
        ):
            with self.subTest(preset=preset):
                run = self.create_run("research", preset)
                self.assertEqual(
                    tuple(
                        run[key]
                        for key in (
                            "max_model_calls",
                            "max_tool_calls",
                            "max_total_tokens",
                            "max_cost_usd",
                        )
                    ),
                    expected[:4],
                )
                elapsed = (
                    datetime.fromisoformat(run["deadline_at"])
                    - datetime.fromisoformat(run["created_at"])
                ).total_seconds()
                self.assertAlmostEqual(elapsed, expected[4], delta=1)

    def test_ordinary_chat_keeps_existing_allowances(self) -> None:
        for preset, expected in (
            ("conservative", (8, 16, 100_000, 2)),
            ("longer", (15, 30, 200_000, 5)),
        ):
            with self.subTest(preset=preset):
                run = self.create_run("chat", preset)
                self.assertEqual(
                    tuple(
                        run[key]
                        for key in (
                            "max_model_calls",
                            "max_tool_calls",
                            "max_total_tokens",
                            "max_cost_usd",
                        )
                    ),
                    expected,
                )

    def test_existing_run_keeps_its_allowances_when_replayed(self) -> None:
        self.db.sql(
            "UPDATE trellis.runs SET max_model_calls=40,max_tool_calls=128,"
            "max_total_tokens=200000,max_cost_usd=5 WHERE id="
            + database_tests.literal(self.db.run_id)
            + ";",
            authenticated=False,
        )
        before = json.loads(
            self.db.sql(
                "SELECT jsonb_build_object('session_id',chat_id,'turn_id',turn_id,"
                "'client_request_id',client_request_id,'model_id',model_id,"
                "'budget_preset',budget_preset,'run_mode',run_mode) FROM trellis.runs WHERE id="
                + database_tests.literal(self.db.run_id)
                + ";"
            ).stdout
        )
        replayed = self.db.rpc(
            "create_run", {**before, "content": "Compare A and B"}, research=False
        )["run"]
        self.assertEqual(replayed["id"], self.db.run_id)
        self.assertEqual(
            tuple(
                replayed[key]
                for key in (
                    "max_model_calls",
                    "max_tool_calls",
                    "max_total_tokens",
                    "max_cost_usd",
                )
            ),
            (40, 128, 200_000, 5),
        )

    def test_larger_reservation_still_uses_the_shared_root_budget(self) -> None:
        payload = {**self.db.reservation(self.db.tasks[0]), "reserved_cost": 6}
        admitted = self.db.rpc("reserve_call", payload)
        self.assertEqual(admitted["reservation"]["reserved_cost"], 6)
        denied = self.db.rpc(
            "reserve_call",
            {**self.db.reservation(self.db.tasks[1]), "reserved_cost": 6},
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("research_finalization_reserve", denied.stderr)


class ResearchBudgetRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_transcripts_and_calls_support_the_extended_budget(self) -> None:
        run = make_run()
        memory = MemoryResearch(run)
        task = memory.task
        user = uuid4()
        for index in range(120):
            await memory.reserve_research_call(
                run.id, run.generation, task.id, str(index), {}, 100, 0.01
            )
        calls = []
        for call in memory.calls:
            row = asdict(call)
            row["user_id"] = str(user)
            row["cached_read_tokens"] = row.pop("cached_tokens")
            calls.append(row)
        messages = [
            {
                "id": str(uuid4()),
                "user_id": str(user),
                "run_id": run.id,
                "task_id": task.id,
                "ordinal": index + 1,
                "role": "assistant" if index < 120 else "tool",
                "content": "Saved research exchange",
                "model_call_id": calls[index]["id"] if index < 120 else None,
                "tool_call_id": None if index < 120 else str(uuid4()),
                "tool_calls": [],
                "continuation": [],
                "created_at": run.created_at,
            }
            for index in range(504)
        ]

        async def handle(request: httpx.Request) -> httpx.Response:
            self.assertTrue(request.url.path.endswith("/rpc/read_research_task"))
            self.assertEqual(
                json.loads(request.content), {"p_run_id": run.id, "p_task_id": task.id}
            )
            return httpx.Response(
                200, json={"task": task.to_dict(), "calls": calls, "messages": messages}
            )

        settings = Settings(
            _env_file=None,
            supabase_url="https://fixture.supabase.co",
            supabase_publishable_key="sb_publishable_fixture",
        )
        tokens = VerifiedTokenSource(user)
        tokens.record_verified("fixture-token", user, datetime.now(UTC) + timedelta(hours=1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            repository = SupabaseRepository(settings, user, tokens, client)
            self.assertEqual(len(await repository.read_research_task_calls(run.id, task.id)), 120)
            self.assertEqual(
                len(await repository.read_research_task_messages(run.id, task.id)), 504
            )
            calls.append(dict(calls[0]))
            with self.assertRaises(RuntimeError):
                await repository.read_research_task_calls(run.id, task.id)
            calls.pop()
            messages.append(dict(messages[0]))
            with self.assertRaises(RuntimeError):
                await repository.read_research_task_messages(run.id, task.id)


if __name__ == "__main__":
    unittest.main()
