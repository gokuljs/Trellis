\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES
    ('23700000-0000-0000-0000-000000000001'),('23700000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES
    ('23700000-0000-0000-0000-000000000001'),('23700000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23700000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
    '23700000-0000-0000-0000-000000000001'::uuid FROM generate_series(80,88) n;
INSERT INTO trellis.messages(user_id,chat_id,turn_id,ordinal,role,content)
    SELECT '23700000-0000-0000-0000-000000000001'::uuid,'23700000-0000-0000-0000-000000000087'::uuid,
        'history-'||((n+1)/2),n,CASE WHEN n%2=1 THEN 'user' ELSE 'assistant' END,'Historical message'
        FROM generate_series(1,300) n;
CREATE TEMP TABLE execution_fixture(n integer PRIMARY KEY,run_id uuid,call_id uuid,model_id text);
GRANT SELECT,INSERT ON execution_fixture TO authenticated;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','23700000-0000-0000-0000-000000000001',true);
DO $$
DECLARE p jsonb; r jsonb; c jsonb; again jsonb;
BEGIN
    FOR n IN 80..88 LOOP
        p:=jsonb_build_object('session_id','23700000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','current','client_request_id','current','content','Current request','request_fingerprint',repeat('a',64),
            'budget_preset','conservative','model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object('run_command','v1'),
                'request_identity','current','installation_id','23700000-0000-0000-0000-000000000099','workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        ASSERT NOT (r->'run' ? 'recovery_stop_owner_digest') AND NOT (r->'run' ? 'recovery_stop_receipt');
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        IF n=87 THEN
            INSERT INTO execution_fixture VALUES(n,(r#>>'{run,id}')::uuid,NULL,p->>'model_id');
            CONTINUE;
        END IF;
        p:=jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,'model_id',p->>'model_id',
            'request_snapshot',jsonb_build_object('purpose',CASE WHEN n=86 THEN 'native_compaction' ELSE 'generation' END,'saved','request'));
        c:=trellis.mutate('create_model_call',p,repeat('a',32));
        again:=trellis.mutate('create_model_call',p,repeat('a',32));
        ASSERT again=c,'lost model allocation acknowledgment must reuse the saved step identity';
        ASSERT (SELECT count(*) FROM trellis.model_calls WHERE run_id=(r#>>'{run,id}')::uuid)=1;
        BEGIN
            PERFORM trellis.mutate('create_model_call',jsonb_set(p,'{request_snapshot,saved}','"changed"'),repeat('a',32));
            RAISE EXCEPTION 'a model step request changed on retry';
        EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        INSERT INTO execution_fixture VALUES(n,(r#>>'{run,id}')::uuid,(c->>'id')::uuid,p->>'model_id');
        IF n IN (80,83,88) THEN CONTINUE; END IF;
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming',
            'input_tokens',10,'output_tokens',3,'accounted_total_tokens',13,'estimated_cost',0.01),repeat('a',32));
        IF n=86 THEN
            PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','failed',
                'error_code','native_context_invalid','response_snapshot',jsonb_build_object('usage_context',jsonb_build_object('usage_complete',true))),repeat('a',32));
        ELSIF n<>82 THEN
            PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',r#>>'{run,id}',
                'model_call_id',c->>'id','batch_index',0,'text','Preserved partial answer'),repeat('a',32));
        END IF;
    END LOOP;
END $$;
DO $$
DECLARE f record; p jsonb; claimed jsonb; first jsonb; second jsonb; stopped jsonb; snapshot jsonb; rows jsonb; count_before integer;
BEGIN
    SELECT * INTO f FROM execution_fixture WHERE n=80;
    p:=jsonb_build_object('run_id',f.run_id);
    first:=trellis.mutate('relinquish_run',p,repeat('a',32));
    ASSERT trellis.mutate('relinquish_run',p,repeat('a',32))=first,
        'lost relinquishment acknowledgment must remain readable after ownership expires';
    claimed:=trellis.mutate('claim_recovery',p||jsonb_build_object('expected_generation',0,
        'installation_id','23700000-0000-0000-0000-000000000099'),repeat('b',32));
    ASSERT (claimed->>'lease_acquired')::boolean AND claimed#>>'{run,status}'='recovering';
    BEGIN
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',f.call_id,'next_status','streaming'),repeat('b',32));
        RAISE EXCEPTION 'a prepared model was dispatched before recovery validation finished';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    first:=trellis.mutate('finish_recovery',p,repeat('b',32));
    second:=trellis.mutate('finish_recovery',p,repeat('b',32));
    ASSERT first=second,'lost recovery completion acknowledgment must reuse the generation lifecycle event';
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id AND event_type='run.resumed')=1;
    BEGIN
        PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',f.run_id,'step_index',2,
            'model_id',f.model_id,'request_snapshot','{}'::jsonb),repeat('a',32));
        RAISE EXCEPTION 'a stale owner allocated another model request';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',f.call_id,'next_status','streaming',
        'input_tokens',21,'output_tokens',7,'accounted_total_tokens',28,'estimated_cost',0.02),repeat('b',32));
    PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',f.run_id,'model_call_id',f.call_id,
        'batch_index',0,'text','Partial output stays saved'),repeat('b',32));
    p:=p||jsonb_build_object('reason','model_outcome_unknown','retry_allowed',true,'usage_unknown',true,'model_call_id',f.call_id,'tool_call_id',NULL);
    stopped:=trellis.mutate('stop_recovery',p,repeat('b',32));
    ASSERT stopped#>>'{run,status}'='interrupted' AND stopped#>>'{run,recovery_reason}'='model_outcome_unknown';
    ASSERT stopped#>'{events,0,data,usage_unknown}'='true'::jsonb;
    ASSERT NOT (stopped->'run' ? 'recovery_stop_receipt') AND NOT (stopped->'run' ? 'recovery_stop_owner_digest');
    snapshot:=trellis.read_recovery_snapshot(f.run_id);
    ASSERT snapshot#>>'{model_calls,0,status}'='failed' AND snapshot#>>'{model_calls,0,error_code}'='model_outcome_unknown';
    ASSERT snapshot#>'{model_calls,0,accounted_total_tokens}'='null'::jsonb AND snapshot#>'{model_calls,0,estimated_cost}'='null'::jsonb;
    ASSERT snapshot#>'{model_calls,0,input_tokens}'='21'::jsonb AND snapshot#>'{model_calls,0,output_tokens}'='7'::jsonb;
    ASSERT snapshot#>>'{outputs,0,status}'='partial' AND snapshot#>>'{outputs,0,content}'='Partial output stays saved';
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.messages WHERE run_id=f.run_id AND role='assistant');
    rows:=trellis.read_session_run_summaries((stopped#>>'{run,chat_id}')::uuid);
    ASSERT rows#>'{0,usage_complete}'='false'::jsonb AND rows#>'{0,cost_complete}'='false'::jsonb;
    count_before:=(SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id);
    ASSERT trellis.mutate('stop_recovery',p,repeat('b',32))=stopped,'lost terminal stop acknowledgment must return the saved receipt';
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id)=count_before;
    BEGIN
        PERFORM trellis.mutate('stop_recovery',p||'{"reason":"changed"}',repeat('b',32));
        RAISE EXCEPTION 'a saved recovery stop changed content';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('stop_recovery',p,repeat('a',32));
        RAISE EXCEPTION 'a stale capability read a terminal recovery receipt';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM set_config('trellis.test_execution_stop',p::text,true);
END $$;
DO $$
DECLARE f record; p jsonb; result jsonb; snapshot jsonb; a jsonb; preview jsonb:='{"approved":"original"}';
BEGIN
    SELECT * INTO f FROM execution_fixture WHERE n=81;
    PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',f.run_id),repeat('a',32));
    p:=jsonb_build_object('run_id',f.run_id,'reason','incompatible_runtime','retry_allowed',true,'usage_unknown',false,
        'model_call_id',f.call_id,'tool_call_id',NULL);
    result:=trellis.mutate('stop_recovery',p,repeat('a',32));
    ASSERT result#>>'{run,status}'='cancelled' AND result#>>'{run,recovery_reason}'='user_cancelled';
    ASSERT result#>'{events,0,data,usage_unknown}'='true'::jsonb,'cancellation must still preserve unknown dispatched usage';
    snapshot:=trellis.read_recovery_snapshot(f.run_id);
    ASSERT snapshot#>'{model_calls,0,accounted_total_tokens}'='null'::jsonb AND snapshot#>'{model_calls,0,estimated_cost}'='null'::jsonb;
    ASSERT snapshot#>'{model_calls,0,input_tokens}'='10'::jsonb AND snapshot#>>'{outputs,0,status}'='partial';
    SELECT * INTO f FROM execution_fixture WHERE n=82;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',f.call_id,'next_status','completed'),repeat('a',32));
    a:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',f.run_id,'model_call_id',f.call_id,
        'message',jsonb_build_object('role','assistant','content','',
            'tool_calls',jsonb_build_array(jsonb_build_object('id','command','name','run_command','arguments','{}'::jsonb)))),repeat('a',32));
    PERFORM trellis.mutate('request_tool_approval',jsonb_build_object('run_id',f.run_id,'tool_call_id',a#>>'{tool_calls,0,id}','preview',preview),repeat('a',32));
    PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object('run_id',f.run_id,'tool_call_id',a#>>'{tool_calls,0,id}','decision','approved'),repeat('a',32));
    PERFORM trellis.mutate('resume_approved_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',f.run_id,'tool_call_id',a#>>'{tool_calls,0,id}',
        'dispatch_id','23700000-0000-0000-0000-000000000821','expected_attempt',0,'recovery_policy','command',
        'input_snapshot',jsonb_build_object('arguments','{}'::jsonb,'approval_preview',preview)),repeat('a',32));
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
    PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,'expected_generation',0,
        'installation_id','23700000-0000-0000-0000-000000000099'),repeat('b',32));
    result:=trellis.mutate('stop_recovery',jsonb_build_object('run_id',f.run_id,'reason','tool_outcome_unknown',
        'retry_allowed',false,'usage_unknown',false,'model_call_id',NULL,'tool_call_id',a#>>'{tool_calls,0,id}'),repeat('b',32));
    ASSERT result#>>'{run,status}'='interrupted' AND result#>'{run,retry_allowed}'='false'::jsonb;
    ASSERT result#>>'{run,uncertain_tool_call_id}'=a#>>'{tool_calls,0,id}';
    ASSERT (SELECT status='outcome_unknown' FROM trellis.tool_calls WHERE id=(a#>>'{tool_calls,0,id}')::uuid);
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE tool_call_id=(a#>>'{tool_calls,0,id}')::uuid);
    SELECT * INTO f FROM execution_fixture WHERE n=83;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',f.run_id,'next_status','interrupted','event_type','run.interrupted'),repeat('a',32));
    BEGIN
        PERFORM trellis.mutate('stop_recovery',jsonb_build_object('run_id',f.run_id,'reason','resume',
            'retry_allowed',true,'usage_unknown',false,'model_call_id',NULL,'tool_call_id',NULL),repeat('a',32));
        RAISE EXCEPTION 'a terminal run was resurrected';
    EXCEPTION WHEN insufficient_privilege OR invalid_parameter_value THEN NULL; END;
    SELECT * INTO f FROM execution_fixture WHERE n=86;
    result:=trellis.mutate('stop_recovery',jsonb_build_object('run_id',f.run_id,'reason','context_changed',
        'retry_allowed',true,'usage_unknown',false,'model_call_id',f.call_id,'tool_call_id',NULL),repeat('a',32));
    ASSERT (SELECT accounted_total_tokens=13 AND estimated_cost=0.01 FROM trellis.model_calls WHERE id=f.call_id),
        'known native fallback spending must be retained';
    SELECT * INTO f FROM execution_fixture WHERE n=87;
    snapshot:=trellis.read_recovery_snapshot(f.run_id);
    ASSERT jsonb_array_length(snapshot->'messages')=129;
    ASSERT snapshot#>'{messages,0,ordinal}'='1'::jsonb AND snapshot#>'{messages,127,ordinal}'='128'::jsonb;
    ASSERT snapshot#>'{messages,128,ordinal}'='301'::jsonb AND snapshot#>>'{messages,128,content}'='Current request',
        'a long history must preserve the exact context head and current input without loading unrelated tail rows';
    SELECT * INTO f FROM execution_fixture WHERE n=88;
    PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',f.run_id),repeat('a',32));
    BEGIN
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',f.call_id,'next_status','streaming'),repeat('a',32));
        RAISE EXCEPTION 'a cancelled prepared model was dispatched';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',f.run_id,'step_index',2,'model_id',f.model_id,
            'request_snapshot','{}'::jsonb),repeat('a',32));
        RAISE EXCEPTION 'a cancelled run prepared another model request';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    ASSERT (SELECT status='pending' FROM trellis.model_calls WHERE id=f.call_id);
END $$;
RESET ROLE;
UPDATE trellis.runs SET deadline_at=now()-interval '1 second',lease_expires_at=now()-interval '1 second'
    WHERE id=(SELECT run_id FROM execution_fixture WHERE n=84);
UPDATE trellis.runs SET recovery_count=3,generation=3,lease_expires_at=now()-interval '1 second'
    WHERE id=(SELECT run_id FROM execution_fixture WHERE n=85);
UPDATE trellis.runs SET recovery_stop_receipt=jsonb_build_object('padding',repeat('x',900000))
    WHERE id=(SELECT run_id FROM execution_fixture WHERE n=80);
SET LOCAL ROLE authenticated;
DO $$
DECLARE f record; result jsonb; snapshot jsonb; rows jsonb;
BEGIN
    FOR f IN SELECT * FROM execution_fixture WHERE n IN (84,85) LOOP
        result:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,
            'expected_generation',CASE WHEN f.n=84 THEN 0 ELSE 3 END,
            'installation_id','23700000-0000-0000-0000-000000000099'),repeat('b',32));
        ASSERT result#>>'{run,status}'='interrupted' AND result->>'rejection_reason'=CASE WHEN f.n=84 THEN 'deadline_exceeded' ELSE 'recovery_limit' END;
        snapshot:=trellis.read_recovery_snapshot(f.run_id);
        ASSERT snapshot#>>'{model_calls,0,error_code}'='model_outcome_unknown';
        ASSERT snapshot#>'{model_calls,0,accounted_total_tokens}'='null'::jsonb AND snapshot#>'{model_calls,0,estimated_cost}'='null'::jsonb;
        ASSERT snapshot#>'{model_calls,0,input_tokens}'='10'::jsonb AND snapshot#>>'{outputs,0,status}'='partial';
        rows:=trellis.read_session_run_summaries((result#>>'{run,chat_id}')::uuid);
        ASSERT rows#>'{0,usage_complete}'='false'::jsonb AND rows#>'{0,cost_complete}'='false'::jsonb;
    END LOOP;
    snapshot:=trellis.read_recovery_snapshot((SELECT run_id FROM execution_fixture WHERE n=80));
    ASSERT octet_length(snapshot::text)<32768 AND NOT (snapshot->'run' ? 'recovery_stop_receipt');
    ASSERT NOT has_column_privilege('authenticated','trellis.runs','recovery_stop_owner_digest','SELECT');
    ASSERT NOT has_function_privilege('authenticated','trellis_private.public_run(jsonb)','EXECUTE');
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_execution_recovery(text,jsonb,text)','EXECUTE');
END $$;
SELECT set_config('request.jwt.claim.sub','23700000-0000-0000-0000-000000000002',true);
DO $$
BEGIN
    BEGIN
        PERFORM trellis.mutate('stop_recovery',current_setting('trellis.test_execution_stop')::jsonb,repeat('b',32));
        RAISE EXCEPTION 'another account read a recovery stop receipt';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
ROLLBACK;
