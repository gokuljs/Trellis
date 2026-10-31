import concurrent.futures
import json
import os
import shutil
import subprocess
import unittest
from dataclasses import asdict
from uuid import uuid4

from app.domain.research import ResearchClaim, ResearchReport, ResearchReview, research_limits
from app.domain.web import CitationReference


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchPostgresTests(unittest.TestCase):
    budget_preset = "conservative"

    def sql(self, statement: str, *, authenticated: bool = True, check: bool = True):
        prefix = (
            f"SET request.jwt.claim.sub={literal(self.owner)}; SET ROLE authenticated;"
            if authenticated
            else ""
        )
        result = subprocess.run(
            [
                shutil.which("psql") or "psql",
                "-X",
                "-qAt",
                "-h127.0.0.1",
                "-p" + os.environ["TRELLIS_TEST_POSTGRES_PORT"],
                "-Upostgres",
                "-d" + os.environ.get("TRELLIS_TEST_POSTGRES_DATABASE", "research_verified"),
                "-vON_ERROR_STOP=1",
            ],
            input=prefix + statement,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if check and result.returncode:
            self.fail(result.stderr)
        return result

    def rpc(self, action: str, payload: dict, *, research: bool = True, check: bool = True):
        function = "research_mutate" if research else "mutate"
        result = self.sql(
            f"SELECT trellis.{function}({literal(action)},"
            + literal(json.dumps(payload))
            + "::jsonb,"
            + literal(self.capability)
            + ");",
            check=check,
        )
        if not check:
            return result
        return json.loads(result.stdout.strip())

    def setUp(self) -> None:
        self.owner = str(uuid4())
        self.capability = "research-test-capability-" + str(uuid4())
        self.sql(f"INSERT INTO auth.users(id) VALUES({literal(self.owner)});", authenticated=False)
        self.sql("SELECT trellis.ensure_account();")
        session = self.rpc("create_session", {}, research=False)
        model = json.loads(
            self.sql(
                "SELECT to_jsonb(id) FROM trellis.models "
                "WHERE enabled ORDER BY catalog_order LIMIT 1;"
            ).stdout
        )
        created = self.rpc(
            "create_run",
            {
                "session_id": session["id"],
                "turn_id": str(uuid4()),
                "client_request_id": str(uuid4()),
                "content": "Compare A and B",
                "model_id": model,
                "budget_preset": self.budget_preset,
                "run_mode": "research",
            },
            research=False,
        )
        self.run_id = created["run"]["id"]
        self.session_id = session["id"]
        self.max_total_tokens = created["run"]["max_total_tokens"]
        self.assertEqual(
            created["run"]["max_model_calls"], research_limits(self.budget_preset).model_calls
        )
        self.rpc(
            "transition_run_record",
            {
                "run_id": self.run_id,
                "next_status": "running",
                "event_type": "run.started",
                "data": {},
            },
            research=False,
        )
        self.rpc("open", {"run_id": self.run_id, "generation": 0, "objective": "Compare A and B"})
        self.tasks = [str(uuid4()), str(uuid4())]
        self.rpc(
            "create_tasks",
            {
                "run_id": self.run_id,
                "generation": 0,
                "operation_id": str(uuid4()),
                "tasks": [
                    {
                        "id": task,
                        "objective": "Investigate product",
                        "role": "worker",
                        "round_index": 1,
                    }
                    for task in self.tasks
                ],
            },
        )

    def tearDown(self) -> None:
        self.sql(f"DELETE FROM auth.users WHERE id={literal(self.owner)};", authenticated=False)

    def reservation(self, task: str, *, tokens: int = 1000) -> dict:
        return {
            "run_id": self.run_id,
            "generation": 0,
            "task_id": task,
            "operation_id": str(uuid4()),
            "request_snapshot": {"purpose": "generation"},
            "reserved_tokens": tokens,
            "reserved_cost": 0.01,
            "finalization": False,
        }

    def test_parallel_reservations_cannot_spend_report_reserve(self) -> None:
        payloads = [
            self.reservation(task, tokens=self.max_total_tokens * 2 // 5) for task in self.tasks
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda payload: self.rpc("reserve_call", payload, check=False), payloads
                )
            )
        self.assertEqual(sum(result.returncode == 0 for result in results), 1)
        failed = next(result for result in results if result.returncode)
        self.assertIn("research_finalization_reserve", failed.stderr)

    def test_reservation_retry_returns_same_call(self) -> None:
        payload = self.reservation(self.tasks[0])
        first = self.rpc("reserve_call", payload)
        second = self.rpc("reserve_call", payload)
        self.assertEqual(first, second)

    def test_checkpoint_replay_converges_after_revision_changes(self) -> None:
        payload = {
            "run_id": self.run_id,
            "generation": 0,
            "task_id": self.tasks[0],
            "expected_revision": 0,
            "operation_id": str(uuid4()),
            "checkpoint": {"findings": [{"id": "pricing", "text": "Saved finding"}]},
        }
        first = self.rpc("checkpoint", payload)
        second = self.rpc("checkpoint", {**payload, "expected_revision": 1})
        self.assertEqual(first, second)
        conflict = self.rpc("checkpoint", {**payload, "checkpoint": {"findings": []}}, check=False)
        self.assertIn("research_operation_conflict", conflict.stderr)

    def test_stale_generation_cannot_dispatch(self) -> None:
        result = self.rpc(
            "reserve_call", {**self.reservation(self.tasks[0]), "generation": 1}, check=False
        )
        self.assertIn("research_scope_invalid", result.stderr)

    def test_unknown_usage_stops_all_branches(self) -> None:
        first = self.rpc("reserve_call", self.reservation(self.tasks[0]))
        self.rpc(
            "mark_unknown",
            {
                "run_id": self.run_id,
                "generation": 0,
                "call_id": first["model_call"]["id"],
                "error_code": "model_outcome_unknown",
            },
        )
        blocked = self.rpc("reserve_call", self.reservation(self.tasks[1]), check=False)
        self.assertIn("research_usage_unknown", blocked.stderr)

    def completed_task(self, *, tool_call: bool = False, tool_name: str = "web_read") -> dict:
        admitted = self.rpc("reserve_call", self.reservation(self.tasks[0]))
        call_id = admitted["model_call"]["id"]
        self.rpc(
            "update_model_call", {"call_id": call_id, "next_status": "streaming"}, research=False
        )
        content = "Private worker finding"
        calls = (
            [
                {
                    "id": "read-" + str(uuid4()),
                    "name": tool_name,
                    "arguments": {"url": "https://example.com/pricing"},
                }
            ]
            if tool_call
            else []
        )
        continuation: list[dict] = []
        response = {
            "content": content,
            "tool_calls": calls,
            "continuation_items": continuation,
            "provider_response_id": "test-response",
            "finish_reason": "tool_calls" if tool_call else "stop",
            "finish_kind": "tool" if tool_call else "final",
        }
        completion = {
            "response_snapshot": response,
            "provider_response_id": "test-response",
            "finish_reason": response["finish_reason"],
            "input_tokens": 100,
            "output_tokens": 20,
            "reasoning_tokens": 0,
            "cached_read_tokens": 0,
            "cache_creation_tokens": 0,
            "estimated_cost": 0.001,
            "accounted_total_tokens": 120,
        }
        return self.rpc(
            "commit_generation",
            {
                "run_id": self.run_id,
                "generation": 0,
                "task_id": self.tasks[0],
                "call_id": call_id,
                "completion": completion,
                "assistant_message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": calls,
                    "tool_call_id": None,
                    "continuation_items": continuation,
                }
                if tool_call
                else None,
                "terminal_result": None if tool_call else {"findings": [content]},
            },
        )

    def seed_source(self, model_call_id: str) -> tuple[str, str]:
        source_id = str(uuid4())
        message_id = str(uuid4())
        tool_id = str(uuid4())
        reservation_id = str(uuid4())
        text = "価格は $10。\nThe price applies to the standard plan."
        text_literal = literal(text)
        usage = {
            "search_calls": 0,
            "page_attempts": 1,
            "network_requests": 1,
            "response_bytes": len(text.encode()),
            "extracted_bytes": len(text.encode()),
        }
        statement = f"""
            INSERT INTO trellis.web_runs(run_id,user_id,session_id,evidence_epoch)
                VALUES({literal(self.run_id)},{literal(self.owner)},{literal(self.session_id)},0);
            INSERT INTO trellis.run_messages(id,user_id,run_id,ordinal,role,content,model_call_id)
                VALUES({literal(message_id)},{literal(self.owner)},{literal(self.run_id)},1,'assistant','',{literal(model_call_id)});
            INSERT INTO trellis.tool_calls(id,user_id,run_id,assistant_message_id,
                call_index,provider_call_id,name,arguments,status)
                VALUES({literal(tool_id)},{literal(self.owner)},{literal(self.run_id)},{literal(message_id)},0,'source-test','web_read','{{}}','completed');
            INSERT INTO trellis.web_reservations(id,user_id,session_id,run_id,tool_call_id,
                dispatch_id,dispatch_attempt,generation,
                evidence_epoch,operation_key,reserved,actual,status)
                VALUES({literal(reservation_id)},{literal(self.owner)},{literal(self.session_id)},{literal(self.run_id)},
                    {literal(tool_id)},{literal(str(uuid4()))},1,0,0,'source',{literal(json.dumps(usage))}::jsonb,{literal(json.dumps(usage))}::jsonb,'settled');
            INSERT INTO trellis.web_sources(id,user_id,session_id,run_id,tool_call_id,
                reservation_id,evidence_epoch,
                requested_url,final_url,canonical_url,title,fetched_at,extractor_version,content_hash,text,line_count,byte_count,status)
                VALUES({literal(source_id)},{literal(self.owner)},{literal(self.session_id)},{literal(self.run_id)},
                    {literal(tool_id)},{literal(reservation_id)},0,'https://example.com/pricing','https://example.com/pricing',
                    'https://example.com/pricing','Pricing',clock_timestamp(),'test-v1',encode(sha256(convert_to({text_literal},'UTF8')),'hex'),
                    {text_literal},2,octet_length({text_literal}),'success')
                RETURNING to_jsonb(content_hash);
        """
        content_hash = json.loads(self.sql(statement, authenticated=False).stdout.strip())
        return source_id, content_hash

    def publication(self) -> dict:
        completion = self.completed_task()
        call_id = completion["model_call"]["id"]
        source_id, content_hash = self.seed_source(call_id)
        claim = ResearchClaim("pricing", "価格は $10。", (CitationReference(source_id, 1, 2),))
        report = ResearchReport(
            f"## Summary\n\n{claim.text} [[source:{source_id}:L1-L2]]\n\n"
            "## Limitations\n\nOnly one pricing source was available.",
            (claim,),
            ("pricing",),
            "coverage_sufficient",
        )
        review = ResearchReview(report.content_hash, {source_id: content_hash}, ("pricing",), ())
        return {
            "run_id": self.run_id,
            "generation": 0,
            "model_call_id": call_id,
            "report": asdict(report),
            "review": asdict(review),
            "report_hash": report.content_hash,
            "partial": False,
        }

    def test_internal_generation_settles_usage_without_publishing_worker_text(self) -> None:
        completed = self.completed_task(tool_call=True)
        saved = completed["model_call"]
        self.assertEqual(saved["accounted_total_tokens"], 120)
        self.assertEqual(len(completed["tool_calls"]), 1)
        self.assertNotIn(
            "assistant.message", [event["event_type"] for event in completed["events"]]
        )
        state = json.loads(
            self.sql(
                "SELECT jsonb_build_object('outputs',"
                "(SELECT count(*) FROM trellis.run_assistant_outputs "
                f"WHERE run_id={literal(self.run_id)}),'leaked_events',"
                "(SELECT count(*) FROM trellis.run_events "
                f"WHERE run_id={literal(self.run_id)} "
                "AND event_type IN ('assistant.delta','assistant.message')),"
                "'usage',(SELECT actual_tokens FROM trellis.research_reservations "
                f"WHERE run_id={literal(self.run_id)}));",
                authenticated=False,
            ).stdout
        )
        self.assertEqual(state, {"outputs": 0, "leaked_events": 0, "usage": 120})

    def test_pause_rejects_dispatched_paid_work(self) -> None:
        self.rpc("reserve_call", self.reservation(self.tasks[0]))
        paused = self.rpc(
            "pause",
            {
                "run_id": self.run_id,
                "generation": 0,
                "clarification_id": str(uuid4()),
                "question": "Which market?",
            },
            check=False,
        )
        self.assertIn("research_not_quiescent", paused.stderr)

    def test_clarification_pauses_clock_and_answer_is_idempotent(self) -> None:
        question_id = str(uuid4())
        paused = self.rpc(
            "pause",
            {
                "run_id": self.run_id,
                "generation": 0,
                "clarification_id": question_id,
                "question": "Which market?",
            },
        )
        self.assertEqual(paused["run"]["status"], "waiting_for_input")
        self.assertEqual(paused["events"][0]["event_type"], "research.clarification_requested")
        snapshot = json.loads(
            self.sql(f"SELECT trellis.read_research({literal(self.run_id)});").stdout
        )
        self.assertGreater(snapshot["remaining_seconds"], 580)
        self.sql(
            "UPDATE trellis.runs SET deadline_at=clock_timestamp()-interval '1 hour' "
            f"WHERE id={literal(self.run_id)};",
            authenticated=False,
        )
        answer_payload = {
            "run_id": self.run_id,
            "clarification_id": question_id,
            "operation_id": str(uuid4()),
            "text": "India",
        }
        answered = self.rpc("answer", answer_payload)
        self.assertEqual(answered["events"][0]["event_type"], "research.clarification_answered")
        replayed = self.rpc("answer", answer_payload)
        self.assertEqual(answered["run"]["deadline_at"], replayed["run"]["deadline_at"])
        self.assertEqual(answered["run"]["generation"], 1)
        self.assertEqual(replayed["run"]["recovery_count"], 0)
        self.assertEqual(
            replayed["run"]["max_model_calls"], research_limits(self.budget_preset).model_calls
        )
        resumed = json.loads(
            self.sql(f"SELECT trellis.read_research({literal(self.run_id)});").stdout
        )
        self.assertGreater(resumed["remaining_seconds"], 570)
        self.assertEqual(resumed["checkpoint"]["clarifications"][0]["answer"], "India")

    def test_cancelled_clarification_cannot_resume_or_extend_deadline(self) -> None:
        question_id = str(uuid4())
        self.rpc(
            "pause",
            {
                "run_id": self.run_id,
                "generation": 0,
                "clarification_id": question_id,
                "question": "Which market?",
            },
        )
        cancelled = self.rpc("request_run_cancellation", {"run_id": self.run_id}, research=False)
        self.assertEqual(cancelled["run"]["status"], "cancelled")
        answer = self.rpc(
            "answer",
            {
                "run_id": self.run_id,
                "clarification_id": question_id,
                "operation_id": str(uuid4()),
                "text": "India",
            },
            check=False,
        )
        self.assertIn("research_clarification_stale", answer.stderr)

    def test_unleased_waiting_run_cancels_through_recovery_route(self) -> None:
        self.rpc(
            "pause",
            {
                "run_id": self.run_id,
                "generation": 0,
                "clarification_id": str(uuid4()),
                "question": "Which market?",
            },
        )
        cancelled = self.rpc(
            "request_recovery_cancellation", {"run_id": self.run_id}, research=False
        )
        self.assertEqual(cancelled["run"]["status"], "cancelled")

    def test_report_publication_is_atomic_and_idempotent_with_unicode_hash(self) -> None:
        payload = self.publication()
        first = self.rpc("publish", payload)
        replayed = self.rpc("publish", payload)
        self.assertEqual(first, replayed)
        self.assertEqual(first["run"]["status"], "completed")
        state = json.loads(
            self.sql(
                "SELECT jsonb_build_object('messages',"
                "(SELECT count(*) FROM trellis.messages "
                f"WHERE run_id={literal(self.run_id)} AND role='assistant'),"
                "'bindings',(SELECT count(*) FROM trellis.web_citations "
                f"WHERE run_id={literal(self.run_id)}),"
                "'outputs',(SELECT count(*) FROM trellis.run_assistant_outputs "
                f"WHERE run_id={literal(self.run_id)} AND promoted_message_id IS NOT NULL));"
            ).stdout
        )
        self.assertEqual(state, {"messages": 1, "bindings": 1, "outputs": 1})

    def test_changed_report_cannot_reuse_review_hash(self) -> None:
        payload = self.publication()
        payload["report"]["markdown"] += "\nA new unsupported assertion."
        rejected = self.rpc("publish", payload, check=False)
        self.assertIn("research_review_changed", rejected.stderr)

    def test_changed_source_hash_prevents_report_publication(self) -> None:
        payload = self.publication()
        source_id = next(iter(payload["review"]["source_hashes"]))
        payload["review"]["source_hashes"][source_id] = "0" * 64
        rejected = self.rpc("publish", payload, check=False)
        self.assertIn("research_source_changed", rejected.stderr)
        self.assertEqual(
            self.sql(
                f"SELECT count(*) FROM trellis.web_citations WHERE run_id={literal(self.run_id)};"
            ).stdout.strip(),
            "0",
        )

    def test_another_account_cannot_read_research(self) -> None:
        owner = self.owner
        self.owner = str(uuid4())
        try:
            result = self.sql(f"SELECT trellis.read_research({literal(self.run_id)}) IS NULL;")
            self.assertEqual(result.stdout.strip(), "t")
        finally:
            self.owner = owner

    def test_snapshot_preserves_meaningful_phase_after_reconnect(self) -> None:
        for phase, expected in (("drafting", "writing"), ("reviewing", "reviewing")):
            self.rpc(
                "append_run_event",
                {"run_id": self.run_id, "event_type": "run.phase", "data": {"phase": phase}},
                research=False,
            )
            self.rpc(
                "append_run_event",
                {"run_id": self.run_id, "event_type": "run.phase", "data": {"phase": "answering"}},
                research=False,
            )
            snapshot = json.loads(
                self.sql(f"SELECT trellis.read_research({literal(self.run_id)});").stdout
            )
            self.assertEqual(snapshot["phase"], expected)


if __name__ == "__main__":
    unittest.main()
