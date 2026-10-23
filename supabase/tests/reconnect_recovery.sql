\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES
    ('23800000-0000-0000-0000-000000000001'),('23800000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES
    ('23800000-0000-0000-0000-000000000001'),('23800000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23800000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
    '23800000-0000-0000-0000-000000000001'::uuid FROM generate_series(90,124) n;
CREATE TEMP TABLE reconnect_fixture(n integer PRIMARY KEY,run_id uuid,call_id uuid,tool_id uuid);
GRANT SELECT,INSERT,UPDATE ON reconnect_fixture TO authenticated;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','23800000-0000-0000-0000-000000000001',true);
DO $$
DECLARE p jsonb; r jsonb; c jsonb; a jsonb; tool_name text; preview jsonb;
BEGIN
    FOR n IN 90..98 LOOP
        p:=jsonb_build_object('session_id','23800000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','reconnect','client_request_id','reconnect','content','Saved request',
            'request_fingerprint',repeat('a',64),'budget_preset','conservative',
            'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1));
        IF n<>95 THEN
            p:=p||jsonb_build_object('recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object('run_command','v1'),
                'request_identity','reconnect','installation_id','23800000-0000-0000-0000-000000000099','workspace',NULL));
        END IF;
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        INSERT INTO reconnect_fixture VALUES(n,(r#>>'{run,id}')::uuid,NULL,NULL);
        IF n NOT IN (90,91,92) THEN CONTINUE; END IF;
        c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}',
            'step_index',1,'model_id',p->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
        UPDATE reconnect_fixture SET call_id=(c->>'id')::uuid WHERE run_id=(r#>>'{run,id}')::uuid;
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming',
            'input_tokens',15,'output_tokens',4,'accounted_total_tokens',19,'estimated_cost',0.01),repeat('a',32));
        IF n=90 THEN
            PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',r#>>'{run,id}','model_call_id',c->>'id',
                'batch_index',0,'text','Preserved unfinished answer'),repeat('a',32));
            CONTINUE;
        END IF;
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','completed'),repeat('a',32));
        tool_name:=CASE WHEN n=91 THEN 'run_command' ELSE 'apply_patch' END;
        preview:=CASE WHEN n=91 THEN '{"command":"build"}'::jsonb ELSE
            jsonb_build_object('target_sha256',repeat('a',64),'desired_sha256',repeat('b',64)) END;
        a:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',r#>>'{run,id}','model_call_id',c->>'id',
            'message',jsonb_build_object('role','assistant','content','',
                'tool_calls',jsonb_build_array(jsonb_build_object('id','local-operation','name',tool_name,'arguments','{}'::jsonb)))),repeat('a',32));
        UPDATE reconnect_fixture SET tool_id=(a#>>'{tool_calls,0,id}')::uuid WHERE run_id=(r#>>'{run,id}')::uuid;
        PERFORM trellis.mutate('request_tool_approval',jsonb_build_object('run_id',r#>>'{run,id}',
            'tool_call_id',a#>>'{tool_calls,0,id}','preview',preview),repeat('a',32));
        PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object('run_id',r#>>'{run,id}',
            'tool_call_id',a#>>'{tool_calls,0,id}','decision','approved'),repeat('a',32));
        PERFORM trellis.mutate('resume_approved_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',r#>>'{run,id}',
            'tool_call_id',a#>>'{tool_calls,0,id}','dispatch_id','23800000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'expected_attempt',0,'recovery_policy',CASE WHEN n=91 THEN 'command' ELSE 'patch' END,
            'input_snapshot',jsonb_build_object('arguments','{}'::jsonb,'approval_preview',preview)),repeat('a',32));
    END LOOP;
END $$;
DO $$
DECLARE f record; p jsonb; result jsonb; again jsonb; original jsonb; events_before bigint;
BEGIN
    FOR f IN SELECT * FROM reconnect_fixture WHERE n IN (90,91,92,94) ORDER BY n LOOP
        original:=trellis.read_recovery_snapshot(f.run_id)->'run';
        PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
        p:=jsonb_build_object('run_id',f.run_id,'installation_id','23800000-0000-0000-0000-000000000098','expected_generation',0);
        IF f.n=94 THEN
            result:=trellis.mutate('claim_recovery',p||'{"expected_generation":1}',NULL);
            ASSERT result->>'rejection_reason'='generation_changed' AND result#>>'{run,status}'='running';
            BEGIN
                PERFORM trellis.mutate('claim_recovery',p-'installation_id',NULL);
                RAISE EXCEPTION 'missing installation identity stopped a run';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        END IF;
        result:=trellis.mutate('claim_recovery',p,NULL);
        ASSERT result->>'rejection_reason'='different_installation' AND result#>>'{run,status}'='interrupted';
        ASSERT result#>>'{run,generation}'='0' AND result#>>'{run,recovery_count}'='0';
        ASSERT result#>'{run,deadline_at}'=original->'deadline_at' AND result#>'{run,max_model_calls}'=original->'max_model_calls';
        ASSERT NOT (result->'run' ? 'lease_token') AND NOT (result->'run' ? 'recovery_stop_receipt');
        events_before:=(SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id);
        again:=trellis.mutate('claim_recovery',p,NULL);
        ASSERT again=result AND (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id)=events_before,
            'a lost policy-rejection acknowledgment must not mutate the terminal run';
        ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_events WHERE run_id=f.run_id AND event_type='run.recovering');
        IF f.n=90 THEN
            ASSERT (SELECT status='failed' AND error_code='model_outcome_unknown' AND input_tokens=15 AND output_tokens=4
                AND accounted_total_tokens IS NULL AND estimated_cost IS NULL FROM trellis.model_calls WHERE id=f.call_id);
            ASSERT (SELECT status='partial' AND content='Preserved unfinished answer'
                FROM trellis.run_assistant_outputs WHERE model_call_id=f.call_id);
            ASSERT trellis.read_session_run_summaries((original->>'chat_id')::uuid)#>'{0,usage_complete}'='false'::jsonb;
            ASSERT (SELECT data->'usage_unknown'='true'::jsonb FROM trellis.run_events
                WHERE run_id=f.run_id AND event_type='run.interrupted');
        ELSIF f.n IN (91,92) THEN
            ASSERT result#>'{run,retry_allowed}'='false'::jsonb AND result#>>'{run,uncertain_tool_call_id}'=f.tool_id::text;
            ASSERT (SELECT status='outcome_unknown' AND dispatch_attempt=1 FROM trellis.tool_calls WHERE id=f.tool_id);
            ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE tool_call_id=f.tool_id);
            ASSERT (SELECT accounted_total_tokens=19 AND estimated_cost=0.01 FROM trellis.model_calls WHERE id=f.call_id),
                'known saved model usage must be retained when a tool outcome is unknown';
        END IF;
    END LOOP;
    ASSERT trellis.mutate('list_recovery_candidates','{"installation_id":"any"}',NULL)='[]'::jsonb;
END $$;
DO $$
DECLARE f record; r jsonb; again jsonb; original jsonb; claim jsonb; intent_at text;
BEGIN
    SELECT * INTO f FROM reconnect_fixture WHERE n=93;
    original:=trellis.read_recovery_snapshot(f.run_id)->'run';
    claim:=jsonb_build_object('run_id',f.run_id,'installation_id','23800000-0000-0000-0000-000000000098','expected_generation',0);
    r:=trellis.mutate('claim_recovery',claim,NULL);
    ASSERT r->>'rejection_reason'='lease_active' AND r#>>'{run,status}'='running',
        'an active foreign owner must never be interrupted by discovery';
    r:=trellis.mutate('request_recovery_cancellation',jsonb_build_object('run_id',f.run_id),NULL);
    ASSERT r#>>'{run,status}'='cancelling' AND r#>>'{event,event_type}'='run.cancellation_requested';
    ASSERT r#>'{run,lease_expires_at}'=original->'lease_expires_at' AND r#>>'{run,generation}'='0';
    intent_at:=r#>>'{run,cancel_requested_at}';
    again:=trellis.mutate('request_recovery_cancellation',jsonb_build_object('run_id',f.run_id),NULL);
    ASSERT again#>>'{run,cancel_requested_at}'=intent_at AND again->'event'='null'::jsonb;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id AND event_type='run.cancellation_requested')=1;
    ASSERT trellis.mutate('claim_recovery',claim,NULL)->>'rejection_reason'='lease_active';
    BEGIN
        PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',f.run_id,'step_index',1,
            'model_id',original->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
        RAISE EXCEPTION 'user cancellation intent allowed a model dispatch';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
    r:=trellis.mutate('claim_recovery',claim,NULL);
    ASSERT r->>'rejection_reason'='user_cancelled' AND r#>>'{run,status}'='cancelled';
    ASSERT r#>>'{run,generation}'='0' AND r#>>'{run,recovery_count}'='0';
    ASSERT trellis.mutate('claim_recovery',claim,NULL)=r;
    again:=trellis.mutate('request_recovery_cancellation',jsonb_build_object('run_id',f.run_id),NULL);
    ASSERT again->'run'=r->'run' AND again->'event'='null'::jsonb,'Stop must not resurrect a terminal run';
    SELECT * INTO f FROM reconnect_fixture WHERE n=96;
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
    PERFORM trellis.mutate('request_recovery_cancellation',jsonb_build_object('run_id',f.run_id),NULL);
    r:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,
        'installation_id','23800000-0000-0000-0000-000000000099','expected_generation',0),repeat('b',32));
    ASSERT r#>'{lease_acquired}'='true'::jsonb AND r#>>'{run,status}'='cancelling',
        'Stop before reconnect claims must prevent replacement execution';
    r:=trellis.mutate('stop_recovery',jsonb_build_object('run_id',f.run_id,'reason','cancellation_requested',
        'retry_allowed',true,'usage_unknown',false,'model_call_id',NULL,'tool_call_id',NULL),repeat('b',32));
    ASSERT r#>>'{run,status}'='cancelled';
END $$;
RESET ROLE;
UPDATE trellis.runs SET lease_expires_at=clock_timestamp()-interval '1 second'
    WHERE id=(SELECT run_id FROM reconnect_fixture WHERE n=95);
UPDATE trellis.runs SET event_storage_count=8192,last_event_sequence=8192
    WHERE id=(SELECT run_id FROM reconnect_fixture WHERE n=97);
SET LOCAL ROLE authenticated;
DO $$
DECLARE r jsonb; f record; other_user text:='23800000-0000-0000-0000-000000000002';
BEGIN
    SELECT * INTO f FROM reconnect_fixture WHERE n=95;
    ASSERT trellis.mutate('list_recovery_candidates','{"installation_id":"any"}',NULL)='[]'::jsonb,
        'legacy runs never enter automatic recovery';
    BEGIN
        PERFORM trellis.mutate('request_recovery_cancellation',jsonb_build_object('run_id',f.run_id),NULL);
        RAISE EXCEPTION 'legacy run silently acquired a recovery contract';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    ASSERT trellis.mutate('recover_expired_run',jsonb_build_object('run_id',f.run_id),NULL)->>'status'='interrupted';
    SELECT * INTO f FROM reconnect_fixture WHERE n=97;
    r:=trellis.mutate('request_recovery_cancellation',jsonb_build_object('run_id',f.run_id),NULL);
    ASSERT r#>>'{run,status}'='cancelling' AND r->'event'='null'::jsonb,'event quota must not lose Stop intent';
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('a',32));
    r:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,
        'installation_id','foreign','expected_generation',0),NULL);
    ASSERT r#>>'{run,status}'='cancelled','event quota must not leave a policy-rejected run busy';
    SELECT * INTO f FROM reconnect_fixture WHERE n=98;
    PERFORM set_config('request.jwt.claim.sub',other_user,true);
    ASSERT trellis.mutate('list_recovery_candidates','{"installation_id":"any"}',NULL)='[]'::jsonb;
    BEGIN
        PERFORM trellis.mutate('request_recovery_cancellation',jsonb_build_object('run_id',f.run_id),NULL);
        RAISE EXCEPTION 'another account stopped the run';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,
            'installation_id','foreign','expected_generation',0),NULL);
        RAISE EXCEPTION 'another account rejected the run';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM set_config('request.jwt.claim.sub','23800000-0000-0000-0000-000000000001',true);
END $$;
DO $$
DECLARE p jsonb; r jsonb; candidates jsonb; rejected integer:=0;
BEGIN
    FOR n IN 100..124 LOOP
        p:=jsonb_build_object('session_id','23800000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','batch','client_request_id','batch','content','Saved request',
            'request_fingerprint',repeat('a',64),'budget_preset','conservative',
            'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions','{}'::jsonb,
                'request_identity','batch','installation_id','23800000-0000-0000-0000-000000000099','workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
    END LOOP;
    candidates:=trellis.mutate('list_recovery_candidates','{"installation_id":"foreign","limit":20}',NULL);
    ASSERT jsonb_array_length(candidates)=20;
    ASSERT NOT (candidates->0 ? 'lease_token') AND NOT (candidates->0 ? 'recovery_stop_owner_digest');
    FOR r IN SELECT value FROM jsonb_array_elements(candidates) LOOP
        PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',r->>'id',
            'installation_id','foreign','expected_generation',r->'generation'),NULL);
        rejected:=rejected+1;
    END LOOP;
    candidates:=trellis.mutate('list_recovery_candidates','{"installation_id":"foreign","limit":20}',NULL);
    ASSERT jsonb_array_length(candidates)=5,'rejected first-page candidates must not starve later runs';
    FOR r IN SELECT value FROM jsonb_array_elements(candidates) LOOP
        PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',r->>'id',
            'installation_id','foreign','expected_generation',r->'generation'),NULL);
        rejected:=rejected+1;
    END LOOP;
    ASSERT rejected=25 AND trellis.mutate('list_recovery_candidates','{"installation_id":"foreign"}',NULL)='[]'::jsonb;
    BEGIN
        PERFORM trellis.mutate('list_recovery_candidates','{"installation_id":"foreign","limit":21}',NULL);
        RAISE EXCEPTION 'unbounded recovery discovery was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('list_recovery_candidates','{"limit":20}',NULL);
        RAISE EXCEPTION 'missing installation identity was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;
ROLLBACK;
