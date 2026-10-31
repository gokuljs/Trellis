import json
import os
import unittest
from datetime import datetime
from uuid import uuid4

from tests import test_research_postgres as database_tests


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchPolicyPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = database_tests.ResearchPostgresTests()
        self.db.budget_preset = "longer"
        self.db.setUp()
        self.addCleanup(self.db.tearDown)

    def v2_transaction(self, body: str) -> dict:
        result = self.db.sql(
            "BEGIN; CREATE OR REPLACE FUNCTION trellis_private.current_research_policy() "
            "RETURNS text LANGUAGE sql IMMUTABLE SET search_path='' AS $$ "
            "SELECT 'bounded-research-v2'::text $$; "
            "SET LOCAL request.jwt.claim.sub=" + database_tests.literal(self.db.owner) + ";"
            "DO $test$ DECLARE v_created jsonb; v_run jsonb; v_first jsonb; v_second jsonb; "
            "v_session uuid; v_task uuid; v_result jsonb; BEGIN " + body + " "
            "PERFORM set_config('test.result',v_result::text,true); END $test$; "
            "SELECT current_setting('test.result')::jsonb; ROLLBACK;",
            authenticated=False,
        )
        return json.loads(result.stdout)

    def create_v2(self, *, session_id: str | None = None, mode: str = "research") -> str:
        payload = {
            "turn_id": str(uuid4()),
            "client_request_id": str(uuid4()),
            "content": "Now compare their methods",
            "budget_preset": "longer",
            "run_mode": mode,
        }
        session = (
            f"v_session:={database_tests.literal(session_id)}::uuid;"
            if session_id is not None
            else "v_created:=trellis_private.mutate('create_session','{}',"
            + database_tests.literal(self.db.capability)
            + "); v_session:=(v_created->>'id')::uuid;"
        )
        return (
            session
            + "v_created:=trellis_private.mutate('create_run',"
            + database_tests.literal(json.dumps(payload))
            + "::jsonb||jsonb_build_object("
            "'session_id',v_session,'model_id',(SELECT model_id FROM trellis.runs WHERE id="
            + database_tests.literal(self.db.run_id)
            + ")),"
            + database_tests.literal(self.db.capability)
            + ");v_run:=v_created->'run';"
        )

    def start_and_open(self) -> str:
        cap = database_tests.literal(self.db.capability)
        return (
            "PERFORM trellis_private.mutate('transition_run_record',jsonb_build_object("
            "'run_id',v_run->>'id','next_status','running','event_type','run.s"
            "tarted','data','{}'::jsonb)," + cap + ");"
            "v_first:=trellis_private.research_mutate('open',jsonb_build_object("
            "'run_id',v_run->>'id','generation',0,'objective','Compare the actual methods'),"
            + cap
            + ");"
        )

    def test_new_extended_profile_is_45_minutes_and_chat_is_unchanged(self) -> None:
        for mode, expected in (("research", 2700), ("chat", 1200)):
            with self.subTest(mode=mode):
                result = self.v2_transaction(self.create_v2(mode=mode) + "v_result:=v_run;")
                if mode == "research":
                    self.assertEqual(result["research_policy_version"], "bounded-research-v2")
                    self.assertEqual(result["execution_budget_seconds"], 2700)
                else:
                    self.assertIsNone(result["execution_budget_seconds"])
                elapsed = (
                    datetime.fromisoformat(result["deadline_at"])
                    - datetime.fromisoformat(result["created_at"])
                ).total_seconds()
                self.assertAlmostEqual(elapsed, expected, delta=1)

    def test_existing_execution_profile_cannot_be_changed(self) -> None:
        denied = self.db.sql(
            "UPDATE trellis.runs SET execution_budget_seconds=2700 WHERE id="
            + database_tests.literal(self.db.run_id)
            + ";",
            authenticated=False,
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("research_scope_invalid", denied.stderr)
        profile = json.loads(
            self.db.sql(
                "SELECT jsonb_build_object('seconds',execution_budget_seconds,'ver"
                "sion',research_policy_version) "
                "FROM trellis.runs WHERE id=" + database_tests.literal(self.db.run_id) + ";"
            ).stdout
        )
        self.assertEqual(profile, {"seconds": 1200, "version": "bounded-research-v1"})

    def test_followup_context_is_frozen_once_before_paid_work(self) -> None:
        statement = (
            self.create_v2()
            + (
                "UPDATE trellis.messages SET ordinal=2 WHERE id=(v_run->>'input_message_id')::uuid;"
                "INSERT INTO trellis.messages(user_id,chat_id,turn_id,ordinal,role,content) VALUES("
                + database_tests.literal(self.db.owner)
                + ",v_session,'prior',1,'user','Compare AutoGen and MetaGPT');"
            )
            + self.start_and_open()
            + (
                "UPDATE trellis.messages SET content='Changed after research start"
                "ed' WHERE chat_id=v_session AND ordinal=1;"
                "v_second:=trellis_private.research_mutate('open',jsonb_build_obje"
                "ct('run_id',v_run->>'id',"
                "'generation',0,'objective','Compare the actual methods'),"
                + database_tests.literal(self.db.capability)
                + ");"
                "v_result:=jsonb_build_object('first',v_first->'checkpoint','secon"
                "d',v_second->'checkpoint',"
                "'paid_calls',(SELECT count(*) FROM trellis.model_calls WHERE run_"
                "id=(v_run->>'id')::uuid));"
            )
        )
        result = self.v2_transaction(statement)
        self.assertEqual(result["first"], result["second"])
        self.assertEqual(
            result["first"]["conversation_context"][0]["content"], "Compare AutoGen and MetaGPT"
        )
        self.assertEqual(result["paid_calls"], 0)

    def test_collection_guard_uses_frozen_45_minute_reserve(self) -> None:
        statement = self.create_v2() + self.start_and_open()
        cap = database_tests.literal(self.db.capability)
        statement += (
            "v_task:=gen_random_uuid(); PERFORM trellis_private.research_mutate('create_tasks',"
            "jsonb_build_object('run_id',v_run->>'id','generation',0,'operatio"
            "n_id',gen_random_uuid(),"
            "'tasks',jsonb_build_array(jsonb_build_object('id',v_task,'objecti"
            "ve','Read methods','role','worker','round_index',1))),"
            + cap
            + "); UPDATE trellis.runs SET deadline_at=clock_timestamp()+interval"
            " '600 seconds' WHERE id=(v_run->>'id')::uuid;"
            "BEGIN PERFORM trellis_private.research_mutate('reserve_call',jsonb_build_object("
            "'run_id',v_run->>'id','generation',0,'task_id',v_task,'operation_"
            "id',gen_random_uuid(),"
            "'request_snapshot','{}'::jsonb,'reserved_tokens',100,'reserved_co"
            "st',0.01,'finalization',false),"
            + cap
            + "); RAISE EXCEPTION 'Expected collection rejection'; EXCEPTION WHE"
            "N invalid_parameter_value THEN "
            "v_result:=jsonb_build_object('reason',SQLERRM); END;"
        )
        self.assertEqual(self.v2_transaction(statement)["reason"], "research_finalization_reserve")

    def test_v2_finding_cannot_cite_another_runs_source(self) -> None:
        call = self.db.rpc("reserve_call", self.db.reservation(self.db.tasks[0]))["model_call"]
        source, _ = self.db.seed_source(call["id"])
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
        cap = database_tests.literal(self.db.capability)
        statement = (
            self.create_v2(session_id=self.db.session_id)
            + self.start_and_open()
            + (
                "v_task:=gen_random_uuid(); PERFORM trellis_private.research_mutate('create_tasks',"
                "jsonb_build_object('run_id',v_run->>'id','generation',0,'operatio"
                "n_id',gen_random_uuid(),"
                "'tasks',jsonb_build_array(jsonb_build_object('id',v_task,'objecti"
                "ve','Read methods','role','worker','round_index',1))),"
                + cap
                + "); BEGIN PERFORM trellis_private.research_mutate('checkpoint',jso"
                "nb_build_object("
                "'run_id',v_run->>'id','generation',0,'task_id',v_task,'operation_"
                "id',gen_random_uuid(),'expected_revision',0,"
                "'checkpoint',jsonb_build_object('findings',jsonb_build_array(json"
                "b_build_object('id','foreign','text','Claim',"
                "'citations',jsonb_build_array(jsonb_build_object('source_id',"
                + database_tests.literal(source)
                + ",'start_line',1,'end_line',2)))))),"
                + cap
                + "); RAISE EXCEPTION 'Expected citation rejection'; "
                "EXCEPTION WHEN invalid_parameter_value THEN v_result:=jsonb_build"
                "_object('reason',SQLERRM); END;"
            )
        )
        self.assertEqual(self.v2_transaction(statement)["reason"], "research_source_changed")
