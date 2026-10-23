\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES
    ('23200000-0000-0000-0000-000000000001'),('23200000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES
    ('23200000-0000-0000-0000-000000000001'),('23200000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23200000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
    '23200000-0000-0000-0000-000000000001'::uuid FROM generate_series(20,24) n;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','23200000-0000-0000-0000-000000000001',true);
DO $$
DECLARE p jsonb; r jsonb; c jsonb; result jsonb; again jsonb; snapshot jsonb;
    response jsonb; completion jsonb; n_events integer;
BEGIN
    p:=jsonb_build_object('session_id','23200000-0000-0000-0000-000000000020',
        'turn_id','final','client_request_id','final','content','hello',
        'request_fingerprint',repeat('a',64),'budget_preset','conservative',
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1));
    r:=trellis.mutate('create_run',p,repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','running','event_type','run.started'),repeat('a',32));
    c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
        'model_id',p->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
    ASSERT NOT (c ? 'completion_owner_digest') AND NOT (c ? 'completion_receipt');
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming'),repeat('a',32));
    PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',r#>>'{run,id}',
        'model_call_id',c->>'id','batch_index',0,'text','Answer'),repeat('a',32));
    response:=jsonb_build_object('content','Answer','tool_calls','[]'::jsonb,'continuation_items','[]'::jsonb,
        'finish_kind','final','finish_reason','stop','provider_response_id','provider-final');
    completion:=jsonb_build_object('response_snapshot',response,'provider_response_id','provider-final',
        'finish_reason','stop','input_tokens',10,'output_tokens',5,'cached_read_tokens',0,'cache_creation_tokens',0,
        'accounted_total_tokens',15,'estimated_cost',0.01);
    p:=jsonb_build_object('run_id',r#>>'{run,id}','call_id',c->>'id','completion',completion,
        'assistant_message',NULL,'final_content','Answer');
    BEGIN
        PERFORM trellis.mutate('commit_generation',p#-'{completion,response_snapshot,finish_kind}',repeat('a',32));
        RAISE EXCEPTION 'missing generation finish kind was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('commit_generation',jsonb_set(p,'{final_content}','42'),repeat('a',32));
        RAISE EXCEPTION 'nontext final content was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    result:=trellis.mutate('commit_generation',p,repeat('a',32));
    ASSERT result#>>'{model_call,status}'='completed' AND result#>>'{model_call,input_tokens}'='10';
    ASSERT result#>>'{model_call,accounted_total_tokens}'='15' AND result#>>'{model_call,estimated_cost}'='0.01';
    ASSERT result#>>'{run,status}'='completed' AND result->'tool_calls'='[]'::jsonb;
    ASSERT jsonb_array_length(result->'events')=3;
    ASSERT result#>>'{events,0,event_type}'='model.completed';
    ASSERT result#>>'{events,1,event_type}'='assistant.completed' AND result#>>'{events,2,event_type}'='run.completed';
    ASSERT NOT (result->'model_call' ? 'completion_owner_digest') AND NOT (result->'model_call' ? 'completion_receipt');
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.runs WHERE id=(r#>>'{run,id}')::uuid AND lease_expires_at IS NOT NULL);
    ASSERT (SELECT count(*) FROM trellis.messages WHERE run_id=(r#>>'{run,id}')::uuid AND role='assistant')=1;
    ASSERT (SELECT status='complete' AND promoted_message_id IS NOT NULL FROM trellis.run_assistant_outputs WHERE model_call_id=(c->>'id')::uuid);
    n_events:=(SELECT count(*) FROM trellis.run_events WHERE run_id=(r#>>'{run,id}')::uuid);
    again:=trellis.mutate('commit_generation',p,repeat('a',32));
    ASSERT again=result,'lost final acknowledgment must return the exact saved receipt after lease clearing';
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=(r#>>'{run,id}')::uuid)=n_events;
    BEGIN
        PERFORM trellis.mutate('commit_generation',p,repeat('b',32));
        RAISE EXCEPTION 'another capability read the receipt';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('commit_generation',jsonb_set(p,'{completion,input_tokens}','11'),repeat('a',32));
        RAISE EXCEPTION 'conflicting usage replaced a saved commit';
    EXCEPTION WHEN invalid_parameter_value THEN
        ASSERT SQLERRM='generation commit conflicts with saved content';
    END;
    snapshot:=trellis.read_recovery_snapshot((r#>>'{run,id}')::uuid);
    ASSERT NOT (snapshot#>'{model_calls,0}' ? 'completion_owner_digest');
    ASSERT NOT (snapshot#>'{model_calls,0}' ? 'completion_receipt');
    ASSERT NOT has_column_privilege('authenticated','trellis.model_calls','completion_owner_digest','SELECT');
    ASSERT NOT has_column_privilege('authenticated','trellis.model_calls','completion_fingerprint','SELECT');
    ASSERT NOT has_column_privilege('authenticated','trellis.model_calls','completion_receipt','SELECT');
    PERFORM set_config('trellis.test_final_payload',p::text,true);
    PERFORM set_config('trellis.test_final_receipt',result::text,true);
END $$;
DO $$
DECLARE p jsonb; r jsonb; c jsonb; response jsonb; assistant jsonb; result jsonb; n_events integer;
BEGIN
    p:=jsonb_build_object('session_id','23200000-0000-0000-0000-000000000021',
        'turn_id','tools','client_request_id','tools','content','inspect',
        'request_fingerprint',repeat('b',64),'budget_preset','conservative',
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1));
    r:=trellis.mutate('create_run',p,repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','running','event_type','run.started'),repeat('a',32));
    c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
        'model_id',p->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming'),repeat('a',32));
    PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',r#>>'{run,id}',
        'model_call_id',c->>'id','batch_index',0,'text','Inspecting'),repeat('a',32));
    response:=jsonb_build_object('content','Inspecting','continuation_items','[]'::jsonb,'finish_kind','tool',
        'finish_reason','tool_calls','provider_response_id','provider-tools','tool_calls',jsonb_build_array(
            jsonb_build_object('id','read-1','name','read_file','arguments',jsonb_build_object('path','a.py')),
            jsonb_build_object('id','read-2','name','read_file','arguments','invalid')));
    assistant:=jsonb_build_object('role','assistant','content','Inspecting','tool_calls',response->'tool_calls',
        'continuation_items','[]'::jsonb,'tool_call_id',NULL);
    p:=jsonb_build_object('run_id',r#>>'{run,id}','call_id',c->>'id',
        'completion',jsonb_build_object('response_snapshot',response,'provider_response_id','provider-tools',
            'finish_reason','tool_calls','input_tokens',20,'output_tokens',8,'cached_read_tokens',0,
            'cache_creation_tokens',0,'accounted_total_tokens',28,'estimated_cost',0.02),
        'assistant_message',assistant,'final_content',NULL);
    n_events:=(SELECT count(*) FROM trellis.run_events WHERE run_id=(r#>>'{run,id}')::uuid);
    BEGIN
        PERFORM trellis.mutate('commit_generation',p,repeat('a',32));
        RAISE EXCEPTION 'invalid tool metadata was committed';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    ASSERT (SELECT status='streaming' AND input_tokens IS NULL FROM trellis.model_calls WHERE id=(c->>'id')::uuid),
        'failure after the model update must roll back usage and completion';
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE run_id=(r#>>'{run,id}')::uuid);
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.tool_calls WHERE run_id=(r#>>'{run,id}')::uuid);
    ASSERT (SELECT status='streaming' FROM trellis.run_assistant_outputs WHERE model_call_id=(c->>'id')::uuid);
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=(r#>>'{run,id}')::uuid)=n_events;
    p:=jsonb_set(jsonb_set(p,'{completion,response_snapshot,tool_calls,1,arguments}','{"path":"b.py"}'),
        '{assistant_message,tool_calls,1,arguments}','{"path":"b.py"}');
    result:=trellis.mutate('commit_generation',p,repeat('a',32));
    ASSERT result#>>'{run,status}'='running' AND jsonb_array_length(result->'tool_calls')=2;
    ASSERT jsonb_array_length(result->'events')=4;
    ASSERT result#>>'{events,0,event_type}'='model.completed' AND result#>>'{events,1,event_type}'='assistant.message';
    ASSERT result#>>'{events,2,event_type}'='tool.call' AND result#>>'{events,3,event_type}'='tool.call';
    ASSERT (SELECT status='complete' FROM trellis.run_assistant_outputs WHERE model_call_id=(c->>'id')::uuid);
    ASSERT (SELECT count(*) FROM trellis.run_messages WHERE run_id=(r#>>'{run,id}')::uuid)=1;
    ASSERT trellis.mutate('commit_generation',p,repeat('a',32))=result;
    ASSERT (SELECT count(*) FROM trellis.tool_calls WHERE run_id=(r#>>'{run,id}')::uuid)=2;
END $$;
DO $$
DECLARE p jsonb; r jsonb; c jsonb; response jsonb; result jsonb;
BEGIN
    FOR n IN 22..23 LOOP
        p:=jsonb_build_object('session_id','23200000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','owned','client_request_id','owned','content','hello',
            'request_fingerprint',repeat('c',64),'budget_preset','conservative',
            'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions','{}'::jsonb,
                'request_identity','owned','installation_id','23200000-0000-0000-0000-000000000099','workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
            'model_id',p->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming'),repeat('a',32));
        response:=jsonb_build_object('content','Answer','tool_calls','[]'::jsonb,'continuation_items','[]'::jsonb,
            'finish_kind','final','finish_reason',NULL,'provider_response_id',NULL);
        p:=jsonb_build_object('run_id',r#>>'{run,id}','call_id',c->>'id',
            'completion',jsonb_build_object('response_snapshot',response,'input_tokens',1,'output_tokens',1),
            'assistant_message',NULL,'final_content','Answer');
        IF n=22 THEN
            PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
            PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
                'installation_id','23200000-0000-0000-0000-000000000099','expected_generation',0),repeat('b',32));
            PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('b',32));
        ELSE
            PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        END IF;
        BEGIN
            PERFORM trellis.mutate('commit_generation',p,repeat('a',32));
            RAISE EXCEPTION 'stale or cancelled execution published a final answer';
        EXCEPTION WHEN insufficient_privilege OR invalid_parameter_value THEN NULL; END;
        ASSERT (SELECT status='streaming' FROM trellis.model_calls WHERE id=(c->>'id')::uuid);
        ASSERT NOT EXISTS(SELECT 1 FROM trellis.messages WHERE run_id=(r#>>'{run,id}')::uuid AND role='assistant');
        IF n=22 THEN
            result:=trellis.mutate('commit_generation',p,repeat('b',32));
            ASSERT result#>>'{run,status}'='completed';
        END IF;
    END LOOP;
END $$;
DO $$
DECLARE p jsonb; r jsonb;
BEGIN
    p:=jsonb_build_object('session_id','23200000-0000-0000-0000-000000000024',
        'turn_id','large-receipts','client_request_id','large-receipts','content','hello',
        'request_fingerprint',repeat('d',64),'budget_preset','conservative',
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1));
    r:=trellis.mutate('create_run',p,repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','running','event_type','run.started'),repeat('a',32));
    FOR n IN 1..2 LOOP
        PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}',
            'step_index',n,'model_id',p->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
    END LOOP;
END $$;
RESET ROLE;
UPDATE trellis.model_calls SET status='completed',response_snapshot='{"content":"small"}'::jsonb,
    completion_fingerprint=repeat('a',64),completion_owner_digest=repeat('b',64),
    completion_receipt=jsonb_build_object('padding',repeat('x',9*1024*1024))
WHERE run_id=(SELECT id FROM trellis.runs WHERE chat_id='23200000-0000-0000-0000-000000000024');
SET LOCAL ROLE authenticated;
DO $$
DECLARE snapshot jsonb; r uuid;
BEGIN
    SELECT id INTO r FROM trellis.runs WHERE chat_id='23200000-0000-0000-0000-000000000024';
    snapshot:=trellis.read_recovery_snapshot(r);
    ASSERT jsonb_array_length(snapshot->'model_calls')=2;
    ASSERT octet_length(snapshot::text)<32768,
        'private receipts must be omitted before enforcing the recovery snapshot byte bound';
    ASSERT NOT (snapshot#>'{model_calls,0}' ? 'completion_receipt');
END $$;
SELECT set_config('request.jwt.claim.sub','23200000-0000-0000-0000-000000000002',true);
DO $$
BEGIN
    BEGIN
        PERFORM trellis.mutate('commit_generation',current_setting('trellis.test_final_payload')::jsonb,repeat('a',32));
        RAISE EXCEPTION 'another account read a saved receipt';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_atomic_generation(text,jsonb,text)','EXECUTE');
    ASSERT NOT has_function_privilege('authenticated','trellis_private.read_recovery_snapshot_before_atomic_generation(uuid)','EXECUTE');
END $$;
ROLLBACK;
