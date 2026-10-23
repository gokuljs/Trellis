\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES
    ('23400000-0000-0000-0000-000000000001'),('23400000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES
    ('23400000-0000-0000-0000-000000000001'),('23400000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23400000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
    '23400000-0000-0000-0000-000000000001'::uuid FROM generate_series(40,47) n;
CREATE TEMP TABLE dispatch_fixture(n integer PRIMARY KEY,run_id uuid NOT NULL,tool_id uuid NOT NULL,input_snapshot jsonb NOT NULL);
GRANT SELECT,INSERT ON dispatch_fixture TO authenticated;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','23400000-0000-0000-0000-000000000001',true);
DO $$
DECLARE p jsonb; r jsonb; c jsonb; a jsonb; tool_name text;
BEGIN
    FOR n IN 40..47 LOOP
        tool_name:=CASE WHEN n IN (41,44) THEN 'run_command' WHEN n IN (42,45) THEN 'apply_patch'
            WHEN n=47 THEN 'custom_action' ELSE 'read_file' END;
        p:=jsonb_build_object('session_id','23400000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','tools','client_request_id','tools','content','Work',
            'request_fingerprint',repeat('a',64),'budget_preset','conservative',
            'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object(tool_name,'v1'),
                'request_identity','tools','installation_id','23400000-0000-0000-0000-000000000099','workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
            'model_id',p->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','completed'),repeat('a',32));
        a:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',r#>>'{run,id}','model_call_id',c->>'id',
            'message',jsonb_build_object('role','assistant','content','',
                'tool_calls',jsonb_build_array(jsonb_build_object('id','tool-1','name',tool_name,
                    'arguments',jsonb_build_object('path','a.py'))))),repeat('a',32));
        ASSERT NOT (a#>'{tool_calls,0}' ? 'dispatch_owner_digest') AND NOT (a#>'{tool_calls,0}' ? 'result_receipt');
        INSERT INTO dispatch_fixture VALUES(n,(r#>>'{run,id}')::uuid,(a#>>'{tool_calls,0,id}')::uuid,
            jsonb_build_object('arguments',jsonb_build_object('path','a.py'),'approval_preview',NULL));
    END LOOP;
END $$;
DO $$
DECLARE f record; p jsonb; started jsonb; saved jsonb; receipt jsonb; count_before integer;
BEGIN
    SELECT * INTO f FROM dispatch_fixture WHERE n=40;
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'dispatch_id','23400000-0000-0000-0000-000000000401','expected_attempt',0,'recovery_policy','read_only','input_snapshot',f.input_snapshot);
    BEGIN
        PERFORM trellis.mutate('record_tool_result',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'content','not dispatched'),repeat('a',32));
        RAISE EXCEPTION 'result without dispatch was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('begin_tool_execution',jsonb_set(p,'{input_snapshot,arguments,path}','"changed.py"'),repeat('a',32));
        RAISE EXCEPTION 'changed tool arguments were dispatched';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    count_before:=(SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id);
    started:=trellis.mutate('begin_tool_execution',p,repeat('a',32));
    ASSERT started#>>'{tool_call,status}'='running' AND started#>>'{tool_call,dispatch_attempt}'='1';
    ASSERT started#>>'{tool_call,dispatch_generation}'='0' AND started#>>'{tool_call,dispatch_id}'=p->>'dispatch_id';
    ASSERT started#>>'{event,event_type}'='run.phase' AND started#>>'{event,data,phase}'='tool_use';
    ASSERT started#>'{tool_call,input_snapshot}'=f.input_snapshot;
    ASSERT trellis.mutate('begin_tool_execution',p,repeat('a',32))=started,'lost dispatch acknowledgment must not allocate another attempt';
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id)=count_before+1;
    BEGIN
        PERFORM trellis.mutate('begin_tool_execution',jsonb_set(p,'{expected_attempt}','1'),repeat('a',32));
        RAISE EXCEPTION 'same dispatch identity accepted changed payload';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    receipt:=trellis.mutate('record_tool_result',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'content','file contents','status','completed','result_source','executor'),repeat('a',32));
    ASSERT receipt#>>'{tool_call,status}'='completed' AND receipt#>>'{tool_call,result_source}'='executor';
    ASSERT receipt#>>'{event,data,result_source}'='executor';
    ASSERT (SELECT count(*) FROM trellis.run_messages WHERE tool_call_id=f.tool_id)=1;
    ASSERT trellis.mutate('begin_tool_execution',p,repeat('a',32))#>>'{tool_call,status}'='completed',
        'a finished dispatch must never authorize replacement execution';
    saved:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'content','file contents','status','completed','result_source','executor');
    ASSERT trellis.mutate('record_tool_result',saved,repeat('a',32))=receipt;
    BEGIN
        PERFORM trellis.mutate('record_tool_result',saved||'{"content":"different"}',repeat('a',32));
        RAISE EXCEPTION 'conflicting tool result was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('record_tool_result',saved,repeat('b',32));
        RAISE EXCEPTION 'another capability read the result receipt';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',f.run_id,'next_status','interrupted','event_type','run.interrupted'),repeat('a',32));
    ASSERT trellis.mutate('record_tool_result',saved,repeat('a',32))=receipt,
        'a lost result acknowledgment stays readable after terminal lease clearing';
    PERFORM set_config('trellis.test_tool_result',saved::text,true);
END $$;
DO $$
DECLARE f record; p jsonb; preview jsonb:='{"approved":"original preview"}'; policy text; claimed jsonb;
BEGIN
    FOR f IN SELECT * FROM dispatch_fixture WHERE n IN (41,42,44,45) ORDER BY n LOOP
        policy:=CASE WHEN f.n IN (41,44) THEN 'command' ELSE 'patch' END;
        p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'dispatch_id','23400000-0000-0000-0000-'||lpad((f.n*10+1)::text,12,'0'),
            'expected_attempt',0,'recovery_policy',policy,'input_snapshot',f.input_snapshot);
        BEGIN
            PERFORM trellis.mutate('begin_tool_execution',p,repeat('a',32));
            RAISE EXCEPTION 'mutation dispatched without approval';
        EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        PERFORM trellis.mutate('request_tool_approval',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'preview',preview),repeat('a',32));
        PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'decision',CASE WHEN f.n=45 THEN 'denied' ELSE 'approved' END),repeat('a',32));
        PERFORM trellis.mutate('resume_approved_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
        p:=jsonb_set(p,'{input_snapshot,approval_preview}',preview);
        IF f.n=45 THEN
            BEGIN
                PERFORM trellis.mutate('begin_tool_execution',p,repeat('a',32));
                RAISE EXCEPTION 'denied mutation was dispatched';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
            PERFORM trellis.mutate('record_tool_result',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
                'content','Denied','status','denied','result_source','approval'),repeat('a',32));
            CONTINUE;
        ELSIF f.n=44 THEN
            PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',f.run_id),repeat('a',32));
            BEGIN
                PERFORM trellis.mutate('begin_tool_execution',p,repeat('a',32));
                RAISE EXCEPTION 'mutation dispatched after cancellation';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
            ASSERT (SELECT dispatch_attempt=0 AND status='pending' FROM trellis.tool_calls WHERE id=f.tool_id);
            CONTINUE;
        END IF;
        BEGIN
            PERFORM trellis.mutate('begin_tool_execution',jsonb_set(p,'{input_snapshot,approval_preview}','{}'),repeat('a',32));
            RAISE EXCEPTION 'changed approval preview was accepted';
        EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        PERFORM trellis.mutate('begin_tool_execution',p,repeat('a',32));
        PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
        claimed:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,'expected_generation',0,
            'installation_id','23400000-0000-0000-0000-000000000099'),repeat('b',32));
        ASSERT (claimed->>'lease_acquired')::boolean;
        PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',f.run_id),repeat('b',32));
        BEGIN
            PERFORM trellis.mutate('begin_tool_execution',p,repeat('a',32));
            RAISE EXCEPTION 'a stale owner was allowed to dispatch';
        EXCEPTION WHEN insufficient_privilege THEN NULL; END;
        BEGIN
            PERFORM trellis.mutate('begin_tool_execution',jsonb_set(jsonb_set(p,'{expected_attempt}','1'),
                '{dispatch_id}',to_jsonb('23400000-0000-0000-0000-'||lpad((f.n*10+2)::text,12,'0'))),repeat('b',32));
            RAISE EXCEPTION 'mutation was automatically repeated';
        EXCEPTION WHEN invalid_parameter_value THEN
            IF f.n=42 THEN ASSERT SQLERRM='patch_reconciliation_required'; END IF;
        END;
        IF f.n=41 THEN
            p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
                'reason','Execution stopped before a confirmed tool result was saved.');
            claimed:=trellis.mutate('mark_tool_outcome_unknown',p,repeat('b',32));
            ASSERT claimed->>'status'='outcome_unknown';
            ASSERT trellis.mutate('mark_tool_outcome_unknown',p,repeat('b',32))=claimed;
            BEGIN
                PERFORM trellis.mutate('mark_tool_outcome_unknown',p||'{"reason":"changed"}',repeat('b',32));
                RAISE EXCEPTION 'unknown reason was replaced';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        END IF;
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',f.run_id,
            'next_status','interrupted','event_type','run.interrupted'),repeat('b',32));
        ASSERT (SELECT status='outcome_unknown' FROM trellis.tool_calls WHERE id=f.tool_id),
            'terminal cleanup must preserve dispatched mutation uncertainty';
        ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE tool_call_id=f.tool_id),
            'unknown outcomes must not receive fabricated tool results';
        ASSERT (SELECT NOT retry_allowed AND uncertain_tool_call_id=f.tool_id AND recovery_reason='tool_outcome_unknown'
            FROM trellis.runs WHERE id=f.run_id);
    END LOOP;
END $$;
DO $$
DECLARE f record; p jsonb; started jsonb;
BEGIN
    SELECT * INTO f FROM dispatch_fixture WHERE n=43;
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'dispatch_id','23400000-0000-0000-0000-000000000431','expected_attempt',0,
        'recovery_policy','read_only','input_snapshot',f.input_snapshot);
    PERFORM trellis.mutate('begin_tool_execution',p,repeat('a',32));
    BEGIN
        PERFORM trellis.mutate('begin_tool_execution',p||'{"expected_attempt":1,"dispatch_id":"23400000-0000-0000-0000-000000000432"}',repeat('a',32));
        RAISE EXCEPTION 'read retry occurred without recovery takeover';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
    PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,'expected_generation',0,
        'installation_id','23400000-0000-0000-0000-000000000099'),repeat('b',32));
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',f.run_id),repeat('b',32));
    BEGIN
        PERFORM trellis.mutate('record_tool_result',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'content','stale output','status','completed'),repeat('a',32));
        RAISE EXCEPTION 'stale owner saved a tool result';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'expected_dispatch_id',p->>'dispatch_id','expected_attempt',1,'reconciliation','read_retry','proof',NULL),repeat('b',32));
    p:=p||'{"expected_attempt":1,"dispatch_id":"23400000-0000-0000-0000-000000000432"}';
    started:=trellis.mutate('begin_tool_execution',p,repeat('b',32));
    ASSERT started#>>'{tool_call,dispatch_attempt}'='2' AND started#>>'{tool_call,dispatch_generation}'='1';
    ASSERT trellis.mutate('begin_tool_execution',p,repeat('b',32))=started;
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('b',32));
    PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,'expected_generation',1,
        'installation_id','23400000-0000-0000-0000-000000000099'),repeat('c',32));
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',f.run_id),repeat('c',32));
    BEGIN
        PERFORM trellis.mutate('begin_tool_execution',p||'{"expected_attempt":2,"dispatch_id":"23400000-0000-0000-0000-000000000433"}',repeat('c',32));
        RAISE EXCEPTION 'read was repeated more than once';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    PERFORM trellis.mutate('mark_tool_outcome_unknown',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'reason','Read retry was lost.'),repeat('c',32));
    ASSERT (SELECT retry_allowed AND uncertain_tool_call_id IS NULL FROM trellis.runs WHERE id=f.run_id);
    SELECT * INTO f FROM dispatch_fixture WHERE n=46;
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'dispatch_id','23400000-0000-0000-0000-000000000461','expected_attempt',0,
        'recovery_policy','read_only','input_snapshot',f.input_snapshot),repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',f.run_id,
        'next_status','interrupted','event_type','run.interrupted'),repeat('a',32));
    ASSERT (SELECT status='cancelled' FROM trellis.tool_calls WHERE id=f.tool_id);
    ASSERT (SELECT retry_allowed FROM trellis.runs WHERE id=f.run_id);
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE tool_call_id=f.tool_id);
    SELECT * INTO f FROM dispatch_fixture WHERE n=47;
    started:=trellis.mutate('create_model_call',jsonb_build_object('run_id',f.run_id,'step_index',2,
        'model_id',(SELECT model_id FROM trellis.runs WHERE id=f.run_id),
        'request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',started->>'id','next_status','streaming'),repeat('a',32));
    BEGIN
        PERFORM trellis.mutate('commit_generation',jsonb_build_object('run_id',f.run_id,'call_id',started->>'id',
            'completion',jsonb_build_object('response_snapshot',jsonb_build_object('content','Premature final',
                'tool_calls','[]'::jsonb,'continuation_items','[]'::jsonb,'finish_kind','final'),
                'input_tokens',1,'output_tokens',1),'assistant_message',NULL,'final_content','Premature final'),repeat('a',32));
        RAISE EXCEPTION 'a final answer completed over an unfinished tool';
    EXCEPTION WHEN invalid_parameter_value THEN
        ASSERT SQLERRM='final generation has unfinished tool operations';
    END;
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.messages WHERE run_id=f.run_id AND role='assistant');
    ASSERT (SELECT status='streaming' AND input_tokens IS NULL FROM trellis.model_calls WHERE id=(started->>'id')::uuid);
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',started->>'id','next_status','cancelled'),repeat('a',32));
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'dispatch_id','23400000-0000-0000-0000-000000000471','expected_attempt',0,
        'recovery_policy','read_only','input_snapshot',NULL);
    BEGIN
        PERFORM trellis.mutate('begin_tool_execution',p,repeat('a',32));
        RAISE EXCEPTION 'unknown tool was declared safe to repeat';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    p:=p||'{"recovery_policy":"never"}';
    started:=trellis.mutate('begin_tool_execution',p,repeat('a',32));
    ASSERT started#>'{tool_call,input_snapshot}'=f.input_snapshot;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',f.run_id,
        'next_status','failed','event_type','run.failed'),repeat('a',32));
    ASSERT (SELECT status='outcome_unknown' FROM trellis.tool_calls WHERE id=f.tool_id);
    ASSERT (SELECT NOT retry_allowed FROM trellis.runs WHERE id=f.run_id);
END $$;
RESET ROLE;
UPDATE trellis.tool_calls SET dispatch_receipt=jsonb_build_object('padding',repeat('x',9*1024*1024)),
    result_receipt=jsonb_build_object('padding',repeat('x',9*1024*1024))
    WHERE id=(SELECT tool_id FROM dispatch_fixture WHERE n=40);
SET LOCAL ROLE authenticated;
DO $$
DECLARE snapshot jsonb;
BEGIN
    snapshot:=trellis.read_recovery_snapshot((SELECT run_id FROM dispatch_fixture WHERE n=40));
    ASSERT octet_length(snapshot::text)<32768,'private dispatch/result receipts must be removed before the snapshot byte bound';
    ASSERT NOT (snapshot#>'{tool_calls,0}' ? 'dispatch_receipt') AND NOT (snapshot#>'{tool_calls,0}' ? 'result_owner_digest');
    ASSERT NOT has_column_privilege('authenticated','trellis.tool_calls','dispatch_owner_digest','SELECT');
    ASSERT NOT has_column_privilege('authenticated','trellis.tool_calls','result_owner_digest','SELECT');
    ASSERT NOT has_function_privilege('authenticated','trellis_private.public_tool_call(jsonb)','EXECUTE');
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_tool_dispatch(text,jsonb,text)','EXECUTE');
END $$;
SELECT set_config('request.jwt.claim.sub','23400000-0000-0000-0000-000000000002',true);
DO $$
BEGIN
    BEGIN
        PERFORM trellis.mutate('record_tool_result',current_setting('trellis.test_tool_result')::jsonb,repeat('a',32));
        RAISE EXCEPTION 'another account read a tool result receipt';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
ROLLBACK;
