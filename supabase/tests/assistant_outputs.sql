\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id,email) VALUES ('00000000-0000-0000-0000-0000000002a1','outputs@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000002a1',true);
SELECT trellis.ensure_account();
DO $check$
DECLARE
    chat uuid; run uuid; call uuid; result jsonb; duplicate jsonb; output uuid;
    cap text := 'Stage2OutputCapabilityLongEnoughForLeaseProtection123';
BEGIN
    result:=trellis.mutate('create_session','{}'::jsonb);
    chat:=(result->>'id')::uuid;
    result:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','output-one',
        'client_request_id','output-one','content','Hi','model_id','openai:gpt-5.5'),cap);
    run:=(result->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','running','event_type','run.started'),cap);
    result:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
        'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
    call:=(result->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','streaming'),cap);
    result:=trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',run,'model_call_id',call,
        'batch_index',0,'text',E' answer\n'),cap);
    output:=(result->'output'->>'id')::uuid;
    duplicate:=trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',run,'model_call_id',call,
        'batch_index',0,'text',E' answer\n'),cap);
    IF duplicate IS DISTINCT FROM result THEN RAISE EXCEPTION 'duplicate changed output or sequence'; END IF;
    BEGIN
        PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',run,'model_call_id',call,
            'batch_index',0,'text','conflict'),cap);
        RAISE EXCEPTION 'conflicting duplicate accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',run,'model_call_id',call,
            'batch_index',2,'text','skip'),cap);
        RAISE EXCEPTION 'out-of-order batch accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',run,'model_call_id',call,
            'batch_index',1,'text','late'),'WrongOutputCapabilityLongEnoughForLeaseProtection123');
        RAISE EXCEPTION 'foreign lease wrote output';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        UPDATE trellis.run_assistant_outputs SET content='forged' WHERE id=output;
        RAISE EXCEPTION 'direct output mutation accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('complete_run',jsonb_build_object('run_id',run,'content','answer'),cap);
        RAISE EXCEPTION 'changed final answer accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','completed'),cap);
    result:=trellis.mutate('finish_assistant_output',jsonb_build_object('run_id',run,'model_call_id',call,
        'stop_reason','tool_continuation'),cap);
    duplicate:=trellis.mutate('finish_assistant_output',jsonb_build_object('run_id',run,'model_call_id',call,
        'stop_reason','tool_continuation'),cap);
    IF duplicate IS DISTINCT FROM result THEN RAISE EXCEPTION 'finalization retry changed output'; END IF;
    result:=trellis.mutate('complete_run',jsonb_build_object('run_id',run,'content',E' answer\n'),cap);
    IF result->'message'->>'content' IS DISTINCT FROM E' answer\n' THEN RAISE EXCEPTION 'answer whitespace changed'; END IF;
    IF result->'events'->0->'data'->>'output_id' IS DISTINCT FROM output::text THEN
        RAISE EXCEPTION 'completion output identity missing';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM trellis.run_assistant_outputs WHERE id=output AND status='complete'
        AND promoted_message_id=(result->'message'->>'id')::uuid) THEN RAISE EXCEPTION 'promotion missing'; END IF;
    result:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','output-two',
        'client_request_id','output-two','content','Hi again','model_id','openai:gpt-5.5'),cap);
    run:=(result->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','running','event_type','run.started'),cap);
    result:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
        'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
    call:=(result->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','streaming'),cap);
    PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',run,'model_call_id',call,
        'batch_index',0,'text','partial'),cap);
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','failed','event_type','run.failed','stop_reason','provider_failure'),cap);
    IF NOT EXISTS(SELECT 1 FROM trellis.run_assistant_outputs WHERE run_id=run AND content='partial'
        AND status='partial' AND stop_reason='provider_failure') THEN RAISE EXCEPTION 'partial output lost'; END IF;
END;
$check$;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000002b1',true);
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM trellis.run_assistant_outputs) THEN RAISE EXCEPTION 'other account read outputs'; END IF;
END; $$;
ROLLBACK;
