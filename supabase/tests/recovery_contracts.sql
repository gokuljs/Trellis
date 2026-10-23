\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES
    ('23000000-0000-0000-0000-000000000001'),('23000000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) SELECT id FROM auth.users WHERE id IN
    ('23000000-0000-0000-0000-000000000001','23000000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23000000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
    '23000000-0000-0000-0000-000000000001'::uuid FROM generate_series(10,16) n;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','23000000-0000-0000-0000-000000000001',true);
DO $$
DECLARE
    p jsonb:=jsonb_build_object('session_id','23000000-0000-0000-0000-000000000010',
        'turn_id','recover','client_request_id','recover','content','hello',
        'request_fingerprint',repeat('a',64),'budget_preset','conservative',
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1));
    contract jsonb:=jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
        'adapter_versions',jsonb_build_object('openai','v1'),
        'tool_versions',jsonb_build_object('read_file','v1'),
        'request_identity','recover','installation_id','23000000-0000-0000-0000-000000000099','workspace',NULL);
    r jsonb; again jsonb; snapshot jsonb;
BEGIN
    r:=trellis.mutate('create_run',p||jsonb_build_object('recovery_contract',contract),repeat('a',32));
    ASSERT r#>'{run,recovery_contract}'=contract;
    ASSERT r#>>'{run,generation}'='0';
    ASSERT NOT (r->'run' ? 'lease_token');
    again:=trellis.mutate('create_run',p||jsonb_build_object('recovery_contract',contract||'{"tool_versions":{}}'),repeat('a',32));
    ASSERT again=r,'acceptance retry must not mutate the immutable contract';
    snapshot:=trellis.read_recovery_snapshot((r#>>'{run,id}')::uuid);
    ASSERT snapshot#>>'{run,id}'=r#>>'{run,id}';
    ASSERT snapshot#>'{run,recovery_contract}'=contract;
    ASSERT NOT (snapshot->'run' ? 'lease_token') AND NOT (snapshot->'run' ? 'request_fingerprint');
    ASSERT jsonb_array_length(snapshot->'messages')=1;
    ASSERT snapshot->'model_calls'='[]'::jsonb AND snapshot->'tool_calls'='[]'::jsonb;
    ASSERT trellis.mutate('list_recovery_candidates',jsonb_build_object('installation_id',contract->>'installation_id'),NULL)='[]'::jsonb,
        'a live execution cannot be discovered for takeover';
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
    BEGIN
        PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',r#>>'{run,id}','event_type','model.started'),repeat('a',32));
        RAISE EXCEPTION 'relinquished owner wrote an event';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    ASSERT jsonb_array_length(trellis.mutate('list_recovery_candidates',jsonb_build_object('installation_id',contract->>'installation_id'),NULL))=1;
    ASSERT jsonb_array_length(trellis.mutate('list_recovery_candidates','{"installation_id":"different"}',NULL))=1,
        'expired foreign installations must be discovered for policy rejection';
    again:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
        'installation_id','different','expected_generation',1),repeat('b',32));
    ASSERT again->>'rejection_reason'='generation_changed';
    BEGIN
        PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
            'installation_id',contract->>'installation_id','expected_generation',0),NULL);
        RAISE EXCEPTION 'missing capability was accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    again:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
        'installation_id',contract->>'installation_id','expected_generation',0),repeat('b',32));
    ASSERT (again->>'lease_acquired')::boolean;
    ASSERT again#>>'{run,id}'=r#>>'{run,id}' AND again#>>'{run,status}'='recovering';
    ASSERT again#>>'{run,generation}'='1' AND again#>>'{run,recovery_count}'='1';
    ASSERT again#>>'{run,deadline_at}'=r#>>'{run,deadline_at}';
    ASSERT again#>>'{run,max_model_calls}'=r#>>'{run,max_model_calls}';
    ASSERT (trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
        'installation_id',contract->>'installation_id','expected_generation',0),repeat('b',32))->>'lease_acquired')::boolean,
        'lost claim acknowledgment must reconcile current ownership';
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=(r#>>'{run,id}')::uuid AND event_type='run.recovering')=1;
    ASSERT NOT (trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
        'installation_id',contract->>'installation_id','expected_generation',0),repeat('c',32))->>'lease_acquired')::boolean;
    BEGIN
        PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',r#>>'{run,id}','event_type','model.started'),repeat('a',32));
        RAISE EXCEPTION 'stale generation wrote an event';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('set_session_workspace',jsonb_build_object('session_id',p->>'session_id','workspace_path','/tmp'),NULL);
        RAISE EXCEPTION 'workspace changed during recovery';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('set_session_model',jsonb_build_object('session_id',p->>'session_id','model_id',p->>'model_id'),NULL);
        RAISE EXCEPTION 'model changed during recovery';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
    again:=trellis.mutate('finish_recovery',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('b',32));
    ASSERT again#>>'{run,status}'='running';
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('b',32));
    again:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
        'installation_id',contract->>'installation_id','expected_generation',0),repeat('c',32));
    ASSERT again->>'rejection_reason'='generation_changed';
    FOR n IN 1..2 LOOP
        again:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
            'installation_id',contract->>'installation_id','expected_generation',n),repeat(chr(98+n),32));
        ASSERT (again->>'lease_acquired')::boolean;
        PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat(chr(98+n),32));
    END LOOP;
    again:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
        'installation_id',contract->>'installation_id','expected_generation',3),repeat('d',32));
    ASSERT again->>'rejection_reason'='recovery_limit';
    ASSERT again#>>'{run,generation}'='3' AND again#>>'{run,recovery_count}'='3';
    ASSERT again#>>'{run,status}'='interrupted' AND again#>>'{run,recovery_reason}'='recovery_limit';
    PERFORM trellis.mutate('create_run',p||jsonb_build_object('session_id','23000000-0000-0000-0000-000000000011'),repeat('a',32));
    BEGIN
        PERFORM trellis.mutate('create_run',p||jsonb_build_object('session_id','23000000-0000-0000-0000-000000000012',
            'recovery_contract',contract||'{"request_identity":"changed"}'),repeat('a',32));
        RAISE EXCEPTION 'contract with wrong request identity was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;
RESET ROLE;
DO $$
DECLARE r trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO r FROM trellis.runs WHERE chat_id='23000000-0000-0000-0000-000000000010';
    BEGIN
        UPDATE trellis.runs SET recovery_contract=recovery_contract||'{"version":2}' WHERE id=r.id;
        RAISE EXCEPTION 'contract changed';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        UPDATE trellis.runs SET recovery_contract=NULL WHERE id=r.id;
        RAISE EXCEPTION 'contract removed';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        UPDATE trellis.runs SET recovery_contract=r.recovery_contract
            WHERE chat_id='23000000-0000-0000-0000-000000000011';
        RAISE EXCEPTION 'legacy run was upgraded implicitly';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
UPDATE trellis.runs SET lease_expires_at=now()-interval '1 second'
    WHERE chat_id='23000000-0000-0000-0000-000000000011';
SET LOCAL ROLE authenticated;
DO $$
DECLARE r trellis.runs%ROWTYPE; result jsonb;
BEGIN
    SELECT id INTO r.id FROM trellis.runs WHERE chat_id='23000000-0000-0000-0000-000000000011';
    result:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r.id,
        'installation_id','23000000-0000-0000-0000-000000000099','expected_generation',0),repeat('a',32));
    ASSERT result->>'rejection_reason'='legacy_run';
    ASSERT trellis.mutate('recover_expired_run',jsonb_build_object('run_id',r.id),NULL)->>'status'='interrupted',
        'legacy expired runs retain conservative interruption';
END $$;
DO $$
DECLARE p jsonb; r jsonb; result jsonb; install text:='23000000-0000-0000-0000-000000000099';
BEGIN
    FOR n IN 12..14 LOOP
        p:=jsonb_build_object('session_id','23000000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','boundary','client_request_id','boundary','content','boundary',
            'request_fingerprint',repeat('b',64),'budget_preset','conservative',
            'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions','{}'::jsonb,
                'request_identity','boundary','installation_id',install,'workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        IF n=12 THEN
            PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        ELSIF n=14 THEN
            PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
                'next_status','interrupted','event_type','run.interrupted'),repeat('a',32));
            result:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
                'installation_id',install,'expected_generation',0),repeat('b',32));
            ASSERT result->>'rejection_reason'='terminal_run';
            CONTINUE;
        END IF;
        PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        IF n=12 THEN
            result:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}',
                'installation_id',install,'expected_generation',0),repeat('b',32));
            ASSERT (result->>'lease_acquired')::boolean AND result#>>'{run,status}'='cancelling',
                'cancellation takes precedence over recovery';
            BEGIN
                PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('b',32));
                RAISE EXCEPTION 'cancelled work resumed';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        END IF;
    END LOOP;
END $$;
RESET ROLE;
UPDATE trellis.runs SET deadline_at=now()-interval '1 second'
    WHERE chat_id='23000000-0000-0000-0000-000000000013';
SELECT set_config('trellis.test_recovery_run',id::text,true) FROM trellis.runs
    WHERE chat_id='23000000-0000-0000-0000-000000000010';
SET LOCAL ROLE authenticated;
DO $$
DECLARE r uuid; result jsonb;
BEGIN
    SELECT id INTO r FROM trellis.runs WHERE chat_id='23000000-0000-0000-0000-000000000013';
    result:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r,
        'installation_id','23000000-0000-0000-0000-000000000099','expected_generation',0),repeat('a',32));
    ASSERT result->>'rejection_reason'='deadline_exceeded';
    ASSERT result#>>'{run,generation}'='0','an expired original budget cannot be reset';
    ASSERT result#>>'{run,status}'='interrupted' AND result#>>'{run,recovery_reason}'='deadline_exceeded';
END $$;
DO $$
DECLARE p jsonb; r jsonb; c jsonb; a jsonb;
BEGIN
    FOR n IN 15..16 LOOP
        p:=jsonb_build_object('session_id','23000000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','edge','client_request_id','edge','content','edge',
            'request_fingerprint',repeat('c',64),'budget_preset','conservative',
            'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object('run_command','v1'),
                'request_identity','edge','installation_id','23000000-0000-0000-0000-000000000099','workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
            'model_id',p->>'model_id','request_snapshot',jsonb_build_object('purpose','generation')),repeat('a',32));
        ASSERT c->>'purpose'='generation';
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming'),repeat('a',32));
        IF n=15 THEN
            PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','completed',
                'response_snapshot','{}'::jsonb,'input_tokens',1,'output_tokens',1),repeat('a',32));
            a:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',r#>>'{run,id}','model_call_id',c->>'id',
                'message',jsonb_build_object('role','assistant','content','',
                    'tool_calls',jsonb_build_array(jsonb_build_object('id','command','name','run_command','arguments','{}'::jsonb)))),repeat('a',32));
        ELSE
            PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',r#>>'{run,id}',
                'model_call_id',c->>'id','batch_index',0,'text','Saved partial output'),repeat('a',32));
        END IF;
        PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
    END LOOP;
END $$;
RESET ROLE;
UPDATE trellis.tool_calls SET status='running',dispatch_attempt=1,dispatch_generation=0,
    dispatched_at=now(),recovery_policy='command'
WHERE run_id=(SELECT id FROM trellis.runs WHERE chat_id='23000000-0000-0000-0000-000000000015');
UPDATE trellis.runs SET deadline_at=now()-interval '1 second'
    WHERE chat_id IN ('23000000-0000-0000-0000-000000000015','23000000-0000-0000-0000-000000000016');
SET LOCAL ROLE authenticated;
DO $$
DECLARE r uuid; result jsonb; snapshot jsonb;
BEGIN
    FOR n IN 15..16 LOOP
        SELECT id INTO r FROM trellis.runs WHERE chat_id=('23000000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid;
        result:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',r,
            'installation_id','23000000-0000-0000-0000-000000000099','expected_generation',0),repeat('b',32));
        ASSERT result#>>'{run,status}'='interrupted';
        snapshot:=trellis.read_recovery_snapshot(r);
        IF n=15 THEN
            ASSERT snapshot#>>'{tool_calls,0,status}'='outcome_unknown','terminal cleanup erased uncertainty';
            ASSERT NOT (result#>>'{run,retry_allowed}')::boolean;
            ASSERT result#>>'{run,uncertain_tool_call_id}'=snapshot#>>'{tool_calls,0,id}';
            ASSERT snapshot->'run_messages'->1 IS NULL,'unknown command must not receive a fabricated result';
            BEGIN
                PERFORM trellis.mutate('create_run',jsonb_build_object('session_id',result#>>'{run,chat_id}',
                    'turn_id','edge','client_request_id','retry-edge','content','edge',
                    'model_id',result#>>'{run,model_id}','budget_preset','conservative',
                    'request_fingerprint',repeat('c',64)),repeat('b',32));
                RAISE EXCEPTION 'unsafe retry was accepted';
            EXCEPTION WHEN invalid_parameter_value THEN
                ASSERT SQLERRM='unsafe_retry_requires_review';
            END;
        ELSE
            ASSERT snapshot#>>'{outputs,0,content}'='Saved partial output';
            ASSERT snapshot#>>'{outputs,0,status}'='partial';
            ASSERT snapshot#>>'{model_calls,0,error_code}'='model_outcome_unknown';
            ASSERT snapshot#>'{model_calls,0,input_tokens}'='null'::jsonb;
            ASSERT snapshot#>'{model_calls,0,output_tokens}'='null'::jsonb;
        END IF;
    END LOOP;
END $$;
SELECT set_config('request.jwt.claim.sub','23000000-0000-0000-0000-000000000002',true);
DO $$
DECLARE r uuid;
BEGIN
    ASSERT trellis.read_recovery_snapshot(current_setting('trellis.test_recovery_run')::uuid) IS NULL;
    ASSERT trellis.mutate('list_recovery_candidates','{"installation_id":"23000000-0000-0000-0000-000000000099"}',NULL)='[]'::jsonb;
    ASSERT NOT has_column_privilege('authenticated','trellis.runs','lease_token','SELECT');
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_recovery_contracts(text,jsonb,text)','EXECUTE');
    BEGIN
        PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',current_setting('trellis.test_recovery_run'),'installation_id','23000000-0000-0000-0000-000000000099','expected_generation',0),repeat('a',32));
        RAISE EXCEPTION 'another account claimed recovery';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
ROLLBACK;
