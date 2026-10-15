\set ON_ERROR_STOP on
-- Upgrade fixture: leave an orphan before migration 010 installs its trigger.
BEGIN;
INSERT INTO auth.users(id,email) VALUES
    ('00000000-0000-0000-0000-0000000010c1','terminal-upgrade@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000010c1',true);
SELECT trellis.ensure_account();
DO $$
DECLARE chat uuid; run uuid; call uuid; r jsonb;
    cap text:='TerminalUpgradeCapabilityWithEnoughRandomCharacters123';
BEGIN
    r:=trellis.mutate('create_session','{}'::jsonb); chat:=(r->>'id')::uuid;
    r:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','upgrade',
        'client_request_id','terminal-model-call-before-010','content','Upgrade',
        'model_id','openai:gpt-5.5'),cap);
    run:=(r->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','running','event_type','run.started'),cap);
    r:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
        'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
    call:=(r->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,
        'next_status','completed','input_tokens',101,'output_tokens',12,
        'estimated_cost',0.007,'response_snapshot',jsonb_build_object('content','Paid response')),cap);
    r:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',2,
        'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
    call:=(r->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,
        'next_status','streaming','input_tokens',64,'output_tokens',8,
        'estimated_cost',0.003,'response_snapshot',jsonb_build_object('usage_context',
            jsonb_build_object('usage_complete',false))),cap);
    PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',3,
        'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','failed','event_type','run.failed'),cap);
END; $$;
COMMIT;
