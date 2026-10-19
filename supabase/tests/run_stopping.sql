\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES ('19200000-0000-0000-0000-000000000001'),('19200000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES ('19200000-0000-0000-0000-000000000001'),('19200000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) VALUES
    ('19200000-0000-0000-0000-000000000010','19200000-0000-0000-0000-000000000001');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','19200000-0000-0000-0000-000000000001',true);
DO $$
DECLARE
    v_status text;
    v_scenario text;
    v_deadline timestamptz;
    v_model text := (SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1);
    v_run uuid;
    v_call uuid;
    v_tool uuid;
    v_record jsonb;
    v_transition jsonb;
BEGIN
    FOREACH v_scenario IN ARRAY ARRAY['interrupted','failed','resumed'] LOOP
        v_status := CASE WHEN v_scenario='resumed' THEN 'interrupted' ELSE v_scenario END;
        v_record := trellis.mutate('create_run',jsonb_build_object(
            'session_id','19200000-0000-0000-0000-000000000010','turn_id',v_scenario,
            'client_request_id',v_scenario,'content','Check approval shutdown.',
            'request_fingerprint',repeat('a',64),'budget_preset','conservative','model_id',v_model),repeat('a',32));
        v_run := (v_record->'run'->>'id')::uuid;
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object(
            'run_id',v_run,'next_status','running','event_type','run.started'),repeat('a',32));
        v_record := trellis.mutate('create_model_call',jsonb_build_object(
            'run_id',v_run,'step_index',1,'model_id',v_model,'request_snapshot','{}'::jsonb),repeat('a',32));
        v_call := (v_record->>'id')::uuid;
        PERFORM trellis.mutate('update_model_call',jsonb_build_object(
            'call_id',v_call,'next_status','streaming'),repeat('a',32));
        PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object(
            'run_id',v_run,'model_call_id',v_call,'batch_index',0,'text','Saved partial answer.'),repeat('a',32));
        PERFORM trellis.mutate('update_model_call',jsonb_build_object(
            'call_id',v_call,'next_status','completed'),repeat('a',32));
        v_record := trellis.mutate('record_assistant_message',jsonb_build_object(
            'run_id',v_run,'model_call_id',v_call,'message',jsonb_build_object(
                'role','assistant','content','Need approval.','tool_calls',jsonb_build_array(
                    jsonb_build_object('id','call-1','name','command','arguments','{}'::jsonb)))),repeat('a',32));
        v_tool := (v_record->'tool_calls'->0->>'id')::uuid;
        PERFORM trellis.mutate('request_tool_approval',jsonb_build_object(
            'run_id',v_run,'tool_call_id',v_tool,'preview','{}'::jsonb),repeat('a',32));
        IF v_scenario='resumed' THEN
            SELECT deadline_at INTO v_deadline FROM trellis.runs WHERE id=v_run;
            PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object(
                'run_id',v_run,'tool_call_id',v_tool,'decision','approved'),repeat('a',32));
            v_record := trellis.mutate('resume_approved_run',jsonb_build_object('run_id',v_run),repeat('a',32));
            ASSERT (v_record->'run'->>'deadline_at')::timestamptz=v_deadline;
            ASSERT (v_record->'event'->'data'->>'deadline_at')::timestamptz=v_deadline;
            ASSERT (SELECT deadline_at=v_deadline FROM trellis.runs WHERE id=v_run),
                'approval waiting must not extend the accepted execution deadline';
        END IF;
        v_transition := jsonb_build_object('run_id',v_run,'next_status',v_status,
            'event_type','run.'||v_status,'stop_reason','runtime_shutdown');
        BEGIN
            PERFORM trellis.mutate('transition_run_record',v_transition,repeat('b',32));
            RAISE EXCEPTION 'wrong capability was accepted';
        EXCEPTION WHEN insufficient_privilege THEN NULL;
        END;
        PERFORM set_config('request.jwt.claim.sub','19200000-0000-0000-0000-000000000002',true);
        BEGIN
            PERFORM trellis.mutate('transition_run_record',v_transition,repeat('a',32));
            RAISE EXCEPTION 'another owner could stop the run';
        EXCEPTION WHEN insufficient_privilege THEN NULL;
        END;
        PERFORM set_config('request.jwt.claim.sub','19200000-0000-0000-0000-000000000001',true);
        PERFORM trellis.mutate('transition_run_record',v_transition,repeat('a',32));
        ASSERT (SELECT status=v_status AND waiting_tool_call_id IS NULL AND lease_expires_at IS NULL
            FROM trellis.runs WHERE id=v_run);
        ASSERT (SELECT status=CASE WHEN v_status='failed' THEN 'failed' ELSE 'cancelled' END
            FROM trellis.tool_calls WHERE id=v_tool);
        ASSERT (SELECT content='Saved partial answer.' AND status='partial'
            FROM trellis.run_assistant_outputs WHERE run_id=v_run);
        ASSERT (SELECT count(*)=1 FROM trellis.run_events WHERE run_id=v_run AND event_type='run.'||v_status);
        BEGIN
            PERFORM trellis.mutate('renew_run_lease',jsonb_build_object('run_id',v_run),repeat('a',32));
            RAISE EXCEPTION 'terminal capability could renew';
        EXCEPTION WHEN insufficient_privilege THEN NULL;
        END;
    END LOOP;
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_run_stopping(text,jsonb,text)','EXECUTE');
END $$;
ROLLBACK;
