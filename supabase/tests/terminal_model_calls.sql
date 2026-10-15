\set ON_ERROR_STOP on
DO $$
DECLARE run trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO STRICT run FROM trellis.runs
    WHERE user_id='00000000-0000-0000-0000-0000000010c1'
        AND client_request_id='terminal-model-call-before-010';
    IF (SELECT count(*) FROM trellis.model_calls c WHERE c.run_id=run.id
            AND c.user_id=run.user_id AND c.step_index IN (2,3) AND c.status='failed'
            AND c.finished_at=run.finished_at AND c.error_code='run_terminated')<>2 THEN
        RAISE EXCEPTION 'migration left historical terminal model-call orphans'; END IF;
    IF NOT EXISTS(SELECT 1 FROM trellis.model_calls c WHERE c.run_id=run.id
            AND c.user_id=run.user_id AND c.step_index=1 AND c.status='completed'
            AND c.input_tokens=101 AND c.output_tokens=12 AND c.estimated_cost=0.007
            AND c.error_code IS NULL AND c.finished_at IS NOT NULL
            AND c.response_snapshot=jsonb_build_object('content','Paid response'))
       OR NOT EXISTS(SELECT 1 FROM trellis.model_calls c WHERE c.run_id=run.id
            AND c.user_id=run.user_id AND c.step_index=2 AND c.input_tokens=64
            AND c.output_tokens=8 AND c.estimated_cost=0.003
            AND c.response_snapshot=jsonb_build_object('usage_context',
                jsonb_build_object('usage_complete',false))) THEN
        RAISE EXCEPTION 'migration changed closed calls or discarded historical spending'; END IF;
END; $$;
DELETE FROM auth.users WHERE id='00000000-0000-0000-0000-0000000010c1';
BEGIN;
INSERT INTO auth.users(id,email) VALUES
    ('00000000-0000-0000-0000-0000000010a1','terminal-a@example.test'),
    ('00000000-0000-0000-0000-0000000010b1','terminal-b@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000010b1',true);
SELECT trellis.ensure_account();
DO $$
DECLARE chat uuid; run uuid; r jsonb;
    cap text:='TerminalForeignCapabilityWithEnoughRandomCharacters123';
BEGIN
    r:=trellis.mutate('create_session','{}'::jsonb); chat:=(r->>'id')::uuid;
    r:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','other',
        'client_request_id','other','content','Other','model_id','openai:gpt-5.5'),cap);
    run:=(r->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','running','event_type','run.started'),cap);
    PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',run,
        'step_index',1,'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
END; $$;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000010a1',true);
SELECT trellis.ensure_account();
DO $$
DECLARE chat uuid; run uuid; pending uuid; streaming uuid; completed uuid;
    r jsonb; before_completed jsonb; before_streaming jsonb; terminal text; finished timestamptz;
    cap text:='TerminalOwnerCapabilityWithEnoughRandomCharacters123';
BEGIN
    FOREACH terminal IN ARRAY ARRAY['failed','cancelled','interrupted','completed'] LOOP
        r:=trellis.mutate('create_session','{}'::jsonb); chat:=(r->>'id')::uuid;
        r:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id',terminal,
            'client_request_id',terminal,'content','Hello','model_id','openai:gpt-5.5'),cap);
        run:=(r->'run'->>'id')::uuid;
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
            'next_status','running','event_type','run.started'),cap);
        r:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
            'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
        completed:=(r->>'id')::uuid;
        before_completed:=trellis.mutate('update_model_call',jsonb_build_object('call_id',completed,
            'next_status','completed','input_tokens',101,'output_tokens',12,
            'estimated_cost',0.007,'response_snapshot',jsonb_build_object('content','Paid response')),cap);
        r:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',2,
            'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
        streaming:=(r->>'id')::uuid;
        before_streaming:=trellis.mutate('update_model_call',jsonb_build_object('call_id',streaming,
            'next_status','streaming','input_tokens',64,'output_tokens',8,
            'estimated_cost',0.003,'response_snapshot',jsonb_build_object('usage_context',
                jsonb_build_object('usage_complete',false))),cap);
        r:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',3,
            'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
        pending:=(r->>'id')::uuid;
        BEGIN
            PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
                'next_status',terminal,'event_type','run.'||terminal),'WrongTerminalCapabilityWithEnoughCharacters123');
            RAISE EXCEPTION 'foreign capability terminated model calls';
        EXCEPTION WHEN insufficient_privilege THEN NULL; END;
        IF (SELECT status FROM trellis.model_calls WHERE id=pending)<>'pending' THEN
            RAISE EXCEPTION 'failed transition modified a pending model call'; END IF;
        IF terminal='failed' THEN
            BEGIN
                PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
                    'next_status','failed','event_type','run.failed',
                    'data',jsonb_build_object('oversized',repeat('x',262145))),cap);
                RAISE EXCEPTION 'oversized terminal event was accepted';
            EXCEPTION WHEN invalid_parameter_value THEN
                IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
            END;
            IF (SELECT status FROM trellis.runs WHERE id=run)<>'running'
               OR (SELECT status FROM trellis.model_calls WHERE id=pending)<>'pending'
               OR (SELECT status FROM trellis.model_calls WHERE id=streaming)<>'streaming' THEN
                RAISE EXCEPTION 'failed terminal event partly finalized run or calls'; END IF;
        ELSIF terminal='cancelled' THEN
            PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',run),cap);
            IF (SELECT status FROM trellis.model_calls WHERE id=pending)<>'pending' THEN
                RAISE EXCEPTION 'nonterminal cancellation request finalized a call'; END IF;
        END IF;
        IF terminal='completed' THEN
            PERFORM trellis.mutate('complete_run',jsonb_build_object('run_id',run,'content','Answer'),cap);
        ELSE
            PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
                'next_status',terminal,'event_type','run.'||terminal),cap);
        END IF;
        SELECT finished_at INTO finished FROM trellis.runs WHERE id=run;
        IF EXISTS(SELECT 1 FROM trellis.model_calls WHERE id IN (pending,streaming)
            AND (status<>CASE WHEN terminal='failed' THEN 'failed' ELSE 'cancelled' END
                OR finished_at IS DISTINCT FROM finished OR error_code IS DISTINCT FROM 'run_terminated')) THEN
            RAISE EXCEPTION 'terminal % left unfinished model calls',terminal; END IF;
        IF (SELECT to_jsonb(c) FROM trellis.model_calls c WHERE id=completed)<>before_completed THEN
            RAISE EXCEPTION 'terminal cleanup changed a completed call'; END IF;
        IF (SELECT to_jsonb(c)-ARRAY['status','finished_at','updated_at','error_code']
                FROM trellis.model_calls c WHERE id=streaming)
            <>before_streaming-ARRAY['status','finished_at','updated_at','error_code'] THEN
            RAISE EXCEPTION 'terminal cleanup lost reported usage or cost'; END IF;
    END LOOP;
END; $$;
RESET ROLE;
-- Recovery has no live process or known model-call ID. Its run update must
-- finalize the orphan in the same transaction while keeping reported usage.
SET LOCAL ROLE authenticated;
DO $$
DECLARE chat uuid; run uuid; call uuid; r jsonb;
    cap text:='TerminalRecoveryCapabilityWithEnoughRandomCharacters123';
BEGIN
    r:=trellis.mutate('create_session','{}'::jsonb); chat:=(r->>'id')::uuid;
    r:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','recovery',
        'client_request_id','recovery','content','Recover','model_id','openai:gpt-5.5'),cap);
    run:=(r->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','running','event_type','run.started'),cap);
    r:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,
        'step_index',1,'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
    call:=(r->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,
        'next_status','streaming','input_tokens',20,'output_tokens',3,'estimated_cost',0.01),cap);
    PERFORM set_config('trellis.test_recovery_run',run::text,true);
END; $$;
RESET ROLE;
UPDATE trellis.runs SET lease_expires_at=now()-interval '1 minute'
WHERE id=current_setting('trellis.test_recovery_run')::uuid;
SET LOCAL ROLE authenticated;
DO $$
DECLARE run uuid:=current_setting('trellis.test_recovery_run')::uuid; r jsonb;
BEGIN
    r:=trellis.mutate('recover_expired_run',jsonb_build_object('run_id',run));
    IF r->>'status'<>'interrupted' OR NOT EXISTS(SELECT 1 FROM trellis.model_calls c
        JOIN trellis.runs parent ON parent.id=c.run_id AND parent.user_id=c.user_id
        WHERE c.run_id=run AND c.status='cancelled' AND c.finished_at=parent.finished_at
            AND c.error_code='run_terminated' AND c.input_tokens=20 AND c.output_tokens=3
            AND c.estimated_cost=0.01) THEN
        RAISE EXCEPTION 'recovery left an orphan or discarded reported usage'; END IF;
END; $$;
RESET ROLE;
DO $$ BEGIN
    IF (SELECT count(*) FROM trellis.model_calls
        WHERE user_id='00000000-0000-0000-0000-0000000010b1' AND status='pending')<>1 THEN
        RAISE EXCEPTION 'terminal cleanup affected another owner'; END IF;
    IF has_function_privilege('authenticated','trellis_private.finalize_run_model_calls()','EXECUTE')
       OR has_function_privilege('anon','trellis_private.finalize_run_model_calls()','EXECUTE') THEN
        RAISE EXCEPTION 'client can execute terminal model-call helper'; END IF;
    IF has_table_privilege('authenticated','trellis.model_calls','UPDATE') THEN
        RAISE EXCEPTION 'client can directly update model calls'; END IF;
END; $$;
ROLLBACK;
