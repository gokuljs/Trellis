\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES ('32000000-0000-0000-0000-000000000001'),('32000000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES ('32000000-0000-0000-0000-000000000001'),('32000000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) VALUES
    ('32000000-0000-0000-0000-000000000010','32000000-0000-0000-0000-000000000001'),
    ('32000000-0000-0000-0000-000000000011','32000000-0000-0000-0000-000000000002');
CREATE FUNCTION pg_temp.web_fixture(p_chat uuid,p_turn text,p_tool text DEFAULT 'web_crawl') RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_run uuid; v_call uuid; v_tool uuid; v_result jsonb; v_dispatch uuid:=gen_random_uuid();
    v_model text:=(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1);
BEGIN
    v_result:=trellis.mutate('create_run',jsonb_build_object('session_id',p_chat,'turn_id',p_turn,
        'client_request_id',p_turn,'content','Read public evidence.','model_id',v_model,
        'budget_preset','conservative','request_fingerprint',repeat('a',64),
        'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
            'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object(p_tool,'v1'),
            'request_identity',p_turn,'installation_id','30000000-0000-0000-0000-000000000099','workspace',NULL)),repeat('a',32));
    v_run:=(v_result->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,'next_status','running','event_type','run.started'),repeat('a',32));
    v_result:=trellis.mutate('create_model_call',jsonb_build_object('run_id',v_run,'step_index',1,
        'model_id',v_model,'request_snapshot','{}'::jsonb),repeat('a',32));
    v_call:=(v_result->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',v_call,'next_status','streaming'),repeat('a',32));
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',v_call,'next_status','completed'),repeat('a',32));
    v_result:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',v_run,'model_call_id',v_call,
        'message',jsonb_build_object('role','assistant','content','Read the page.','tool_calls',jsonb_build_array(
            jsonb_build_object('id','fixture-call','name',p_tool,'arguments','{}'::jsonb)))),repeat('a',32));
    v_tool:=(v_result->'tool_calls'->0->>'id')::uuid;
    IF p_tool='run_command' THEN
        PERFORM trellis.mutate('request_tool_approval',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,'preview','{}'::jsonb),repeat('a',32));
        PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,'decision','approved'),repeat('a',32));
        PERFORM trellis.mutate('resume_approved_run',jsonb_build_object('run_id',v_run),repeat('a',32));
    END IF;
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
        'dispatch_id',v_dispatch,'expected_attempt',0,'recovery_policy',CASE WHEN p_tool='run_command' THEN 'command' ELSE 'checkpointed' END,
        'input_snapshot',NULL),repeat('a',32));
    RETURN jsonb_build_object('user_id',auth.uid(),'session_id',p_chat,'run_id',v_run,'tool_call_id',v_tool,
        'dispatch_id',v_dispatch,'dispatch_attempt',1,'generation',0,'evidence_epoch',0,'model_call_id',v_call);
END;
$$;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','32000000-0000-0000-0000-000000000001',true);
DO $$
DECLARE v_scope jsonb; v_other jsonb; v_result jsonb;
    v_request uuid:='32000000-0000-0000-0000-000000000020';
    v_other_request uuid:='32000000-0000-0000-0000-000000000021';
    v_usage jsonb:='{"search_calls":1,"page_attempts":0,"network_requests":1,"response_bytes":1048576,"extracted_bytes":0}';
    v_hash text:=repeat('c',64); v_i integer;
BEGIN
    ASSERT trellis.web_readiness()='{"schema_version":3,"ready":true}'::jsonb;
    v_scope:=pg_temp.web_fixture('32000000-0000-0000-0000-000000000010','meter-a','web_search')-'model_call_id';
    PERFORM trellis.web_mutate('open_run',v_scope,repeat('a',32));
    BEGIN
        PERFORM trellis.web_reserve_search_attempt(v_scope,v_request,v_hash,2,repeat('a',32));
        RAISE EXCEPTION 'search was metered without an upfront run reservation';
    EXCEPTION WHEN insufficient_privilege THEN ASSERT SQLERRM='web_scope_invalid'; END;
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_request,'operation_key','search:0','usage',v_usage),repeat('a',32));
    v_result:=trellis.web_reserve_search_attempt(v_scope,v_request,v_hash,2,repeat('a',32));
    ASSERT v_result='{"reserved":true}'::jsonb;
    ASSERT trellis.web_reserve_search_attempt(v_scope,v_request,v_hash,2,repeat('a',32))=v_result;
    BEGIN
        PERFORM trellis.web_reserve_search_attempt(v_scope,v_request,repeat('d',64),2,repeat('a',32));
        RAISE EXCEPTION 'a request changed its provider key';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_source_conflict'; END;
    BEGIN
        PERFORM trellis.web_reserve_search_attempt(v_scope||'{"generation":1}',v_request,v_hash,2,repeat('a',32));
        RAISE EXCEPTION 'stale generation metered a request';
    EXCEPTION WHEN insufficient_privilege THEN ASSERT SQLERRM='web_scope_invalid'; END;
    BEGIN
        PERFORM trellis.web_reserve_search_attempt(v_scope||'{"evidence_epoch":1}',v_request,v_hash,2,repeat('a',32));
        RAISE EXCEPTION 'stale evidence epoch metered a request';
    EXCEPTION WHEN insufficient_privilege THEN ASSERT SQLERRM='web_scope_invalid'; END;
    BEGIN
        PERFORM trellis.web_reserve_search_attempt(v_scope,v_request,v_hash,2,repeat('b',32));
        RAISE EXCEPTION 'wrong lease capability metered a request';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM trellis.web_reserve_search_attempt(v_scope,v_request,v_hash,1001,repeat('a',32));
        RAISE EXCEPTION 'operator ceiling exceeded hard maximum';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_scope_invalid'; END;
    BEGIN
        EXECUTE 'SELECT attempts FROM trellis_private.web_search_months';
        RAISE EXCEPTION 'authenticated caller read shared provider usage';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM set_config('request.jwt.claim.sub','32000000-0000-0000-0000-000000000002',true);
    BEGIN
        PERFORM trellis.web_reserve_search_attempt(v_scope,v_request,v_hash,2,repeat('a',32));
        RAISE EXCEPTION 'another owner metered a request';
    EXCEPTION WHEN insufficient_privilege THEN ASSERT SQLERRM='web_scope_invalid'; END;
    v_other:=pg_temp.web_fixture('32000000-0000-0000-0000-000000000011','meter-b','web_search')-'model_call_id';
    PERFORM trellis.web_mutate('open_run',v_other,repeat('a',32));
    PERFORM trellis.web_mutate('reserve',v_other||jsonb_build_object('reservation_id',v_other_request,'operation_key','search:0','usage',v_usage),repeat('a',32));
    ASSERT trellis.web_reserve_search_attempt(v_other,v_other_request,v_hash,2,repeat('a',32))=v_result;
    PERFORM set_config('request.jwt.claim.sub','32000000-0000-0000-0000-000000000001',true);
    FOR v_i IN 1..4 LOOP
        PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',gen_random_uuid(),'operation_key','search:'||v_i,'usage',v_usage),repeat('a',32));
    END LOOP;
    BEGIN
        PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',gen_random_uuid(),'operation_key','search:5','usage',v_usage),repeat('a',32));
        RAISE EXCEPTION 'sixth search attempt was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_budget_exhausted'; END;
    BEGIN
        PERFORM trellis.web_reserve_search_attempt(v_scope,
            (SELECT id FROM trellis.web_reservations WHERE run_id=(v_scope->>'run_id')::uuid AND operation_key='search:1'),v_hash,2,repeat('a',32));
        RAISE EXCEPTION 'third shared-key attempt was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_search_budget_exhausted'; END;
    ASSERT trellis.web_reserve_search_attempt(v_scope,v_request,v_hash,2,repeat('a',32))=v_result;
END;
$$;
RESET ROLE;
DO $$
BEGIN
    ASSERT (SELECT attempts=2 FROM trellis_private.web_search_months WHERE key_hash=repeat('c',64)
        AND month_start=date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC')::date);
    ASSERT (SELECT count(*)=2 FROM trellis_private.web_search_attempts WHERE key_hash=repeat('c',64));
    ASSERT NOT has_table_privilege('authenticated','trellis_private.web_search_months','SELECT');
    ASSERT NOT has_table_privilege('authenticated','trellis_private.web_search_attempts','SELECT');
END;
$$;
ROLLBACK;
