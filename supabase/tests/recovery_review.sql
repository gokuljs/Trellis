\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES
    ('23600000-0000-0000-0000-000000000001'),('23600000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES
    ('23600000-0000-0000-0000-000000000001'),('23600000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23600000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
    '23600000-0000-0000-0000-000000000001'::uuid FROM generate_series(70,74) n;
CREATE TEMP TABLE review_fixture(n integer PRIMARY KEY,session_id uuid,run_id uuid,tool_id uuid);
GRANT SELECT,INSERT ON review_fixture TO authenticated;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','23600000-0000-0000-0000-000000000001',true);
DO $$
DECLARE p jsonb; r jsonb; c jsonb; a jsonb; event jsonb; tool_name text; preview jsonb:='{"approved":"original"}';
BEGIN
    FOR n IN 70..74 LOOP
        tool_name:=CASE WHEN n=72 THEN 'apply_patch' ELSE 'run_command' END;
        p:=jsonb_build_object('session_id','23600000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','review','client_request_id','review','content','Work','request_fingerprint',repeat('a',64),
            'budget_preset','conservative','model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object(tool_name,'v1'),
                'request_identity','review','installation_id','23600000-0000-0000-0000-000000000099','workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
            'model_id',p->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','completed'),repeat('a',32));
        a:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',r#>>'{run,id}','model_call_id',c->>'id',
            'message',jsonb_build_object('role','assistant','content','',
                'tool_calls',jsonb_build_array(jsonb_build_object('id','tool-1','name',tool_name,
                    'arguments',jsonb_build_object('command','example'))))),repeat('a',32));
        PERFORM trellis.mutate('request_tool_approval',jsonb_build_object('run_id',r#>>'{run,id}',
            'tool_call_id',a#>>'{tool_calls,0,id}','preview',preview),repeat('a',32));
        PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object('run_id',r#>>'{run,id}',
            'tool_call_id',a#>>'{tool_calls,0,id}','decision','approved'),repeat('a',32));
        PERFORM trellis.mutate('resume_approved_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',r#>>'{run,id}',
            'tool_call_id',a#>>'{tool_calls,0,id}',
            'dispatch_id','23600000-0000-0000-0000-'||lpad((n*10+1)::text,12,'0'),'expected_attempt',0,
            'recovery_policy',CASE WHEN n=72 THEN 'patch' ELSE 'command' END,
            'input_snapshot',jsonb_build_object('arguments',jsonb_build_object('command','example'),'approval_preview',preview)),repeat('a',32));
        INSERT INTO review_fixture VALUES(n,(p->>'session_id')::uuid,(r#>>'{run,id}')::uuid,(a#>>'{tool_calls,0,id}')::uuid);
        IF n=71 THEN
            PERFORM trellis.mutate('mark_tool_outcome_unknown',jsonb_build_object('run_id',r#>>'{run,id}',
                'tool_call_id',a#>>'{tool_calls,0,id}','reason','The command result was lost.'),repeat('a',32));
            PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
            PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}','expected_generation',0,
                'installation_id','23600000-0000-0000-0000-000000000099'),repeat('b',32));
            CONTINUE;
        ELSIF n=73 THEN
            PERFORM trellis.mutate('record_tool_result',jsonb_build_object('run_id',r#>>'{run,id}',
                'tool_call_id',a#>>'{tool_calls,0,id}','content','Confirmed exit.','status','completed','result_source','executor'),repeat('a',32));
        ELSIF n=74 THEN
            PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        END IF;
        event:=trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status',CASE WHEN n=74 THEN 'cancelled' WHEN n=72 THEN 'failed' ELSE 'interrupted' END,
            'event_type',CASE WHEN n=74 THEN 'run.cancelled' WHEN n=72 THEN 'run.failed' ELSE 'run.interrupted' END,
            'data',jsonb_build_object('retry_allowed',true,'recovery_reason','untrusted',
                'uncertain_tool_call_id','23600000-0000-0000-0000-000000000999')),repeat('a',32));
        IF n<>73 THEN
            ASSERT event#>'{data,retry_allowed}'='false'::jsonb,
                'the terminal event must include uncertainty discovered by terminal cleanup';
            ASSERT event#>>'{data,uncertain_tool_call_id}'=a#>>'{tool_calls,0,id}';
            ASSERT event#>>'{data,recovery_reason}'='tool_outcome_unknown';
        ELSE
            ASSERT event#>'{data,retry_allowed}'='true'::jsonb;
            ASSERT event#>'{data,uncertain_tool_call_id}'='null'::jsonb;
            ASSERT event#>'{data,recovery_reason}'='null'::jsonb;
        END IF;
    END LOOP;
END $$;
DO $$
DECLARE f record; p jsonb; result jsonb; again jsonb; rows jsonb; count_before integer;
BEGIN
    SELECT * INTO f FROM review_fixture WHERE n=70;
    p:=jsonb_build_object('session_id',f.session_id,'run_id',f.run_id,'tool_call_id',f.tool_id);
    count_before:=(SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id);
    result:=trellis.mutate('acknowledge_workspace_review',p,NULL);
    ASSERT result->>'reviewed_at' IS NOT NULL;
    ASSERT result->>'status'='outcome_unknown' AND result->>'dispatch_attempt'='1';
    ASSERT result->>'result_source'='run_terminated_after_dispatch';
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE tool_call_id=f.tool_id),
        'review must neither execute the command nor manufacture a successful result';
    ASSERT (SELECT status='interrupted' AND NOT retry_allowed AND uncertain_tool_call_id=f.tool_id FROM trellis.runs WHERE id=f.run_id);
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id)=count_before+1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id
        AND event_type='run.phase' AND data->>'review_acknowledged'='true' AND data->>'tool_call_id'=f.tool_id::text)=1;
    again:=trellis.mutate('acknowledge_workspace_review',p,NULL);
    ASSERT again=result,'duplicate review acknowledgment must preserve the original timestamp';
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id)=count_before+1;
    rows:=trellis.read_session_run_summaries(f.session_id);
    ASSERT rows#>'{0,run,retry_allowed}'='false'::jsonb;
    ASSERT rows#>>'{0,run,recovery_reason}'='tool_outcome_unknown';
    ASSERT rows#>>'{0,run,uncertain_tool_call_id}'=f.tool_id::text;
    ASSERT rows#>'{0,run,generation}'='0'::jsonb;
    ASSERT NOT (rows#>'{0,run}' ? 'lease_token');
    BEGIN
        PERFORM trellis.mutate('create_run',jsonb_build_object('session_id',f.session_id,'turn_id','review',
            'client_request_id','retry-after-review','content','Work','budget_preset','conservative',
            'model_id',(SELECT model_id FROM trellis.runs WHERE id=f.run_id),'request_fingerprint',repeat('a',64)),repeat('c',32));
        RAISE EXCEPTION 'review reenabled unsafe generic Retry';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='unsafe_retry_requires_review'; END;
    BEGIN
        PERFORM trellis.mutate('acknowledge_workspace_review',p||'{"session_id":"23600000-0000-0000-0000-000000000071"}',NULL);
        RAISE EXCEPTION 'review accepted the wrong chat';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM set_config('trellis.test_workspace_review',p::text,true);
    FOR f IN SELECT * FROM review_fixture WHERE n IN (71,72,73) LOOP
        BEGIN
            PERFORM trellis.mutate('acknowledge_workspace_review',jsonb_build_object(
                'session_id',f.session_id,'run_id',f.run_id,'tool_call_id',f.tool_id),NULL);
            RAISE EXCEPTION 'review accepted active work or a different operation type/outcome';
        EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        ASSERT (SELECT reviewed_at IS NULL FROM trellis.tool_calls WHERE id=f.tool_id);
    END LOOP;
    SELECT * INTO f FROM review_fixture WHERE n=71;
    rows:=trellis.read_session_run_summaries(f.session_id);
    ASSERT rows#>>'{0,run,status}'='recovering' AND rows#>'{0,run,generation}'='1'::jsonb;
    ASSERT rows#>'{0,run,max_model_calls}'='8'::jsonb AND rows#>'{0,run,max_tool_calls}'='16'::jsonb;
    ASSERT rows#>'{0,run,retry_allowed}'='false'::jsonb;
END $$;
RESET ROLE;
INSERT INTO trellis.run_events(user_id,run_id,sequence,event_type,data)
    SELECT r.user_id,r.id,n,'run.phase','{}'::jsonb
    FROM trellis.runs r CROSS JOIN LATERAL generate_series(r.last_event_sequence+1,8192) n
    WHERE r.id=(SELECT run_id FROM review_fixture WHERE n=74);
UPDATE trellis.runs SET last_event_sequence=8192,event_storage_count=8192,
    event_storage_bytes=(SELECT sum(octet_length(e.data::text)) FROM trellis.run_events e WHERE e.run_id=trellis.runs.id)
    WHERE id=(SELECT run_id FROM review_fixture WHERE n=74);
SET LOCAL ROLE authenticated;
DO $$
DECLARE f record; result jsonb;
BEGIN
    SELECT * INTO f FROM review_fixture WHERE n=74;
    result:=trellis.mutate('acknowledge_workspace_review',jsonb_build_object(
        'session_id',f.session_id,'run_id',f.run_id,'tool_call_id',f.tool_id),NULL);
    ASSERT result->>'reviewed_at' IS NOT NULL,'an exhausted replay quota must not prevent explicit workspace review';
    ASSERT result->>'status'='outcome_unknown';
    ASSERT (SELECT NOT retry_allowed FROM trellis.runs WHERE id=f.run_id);
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id)=8192;
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_recovery_review(text,jsonb,text)','EXECUTE');
END $$;
SELECT set_config('request.jwt.claim.sub','23600000-0000-0000-0000-000000000002',true);
DO $$
BEGIN
    BEGIN
        PERFORM trellis.mutate('acknowledge_workspace_review',current_setting('trellis.test_workspace_review')::jsonb,NULL);
        RAISE EXCEPTION 'another account acknowledged the workspace command';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    ASSERT trellis.read_session_run_summaries('23600000-0000-0000-0000-000000000070')='[]'::jsonb;
END $$;
ROLLBACK;
