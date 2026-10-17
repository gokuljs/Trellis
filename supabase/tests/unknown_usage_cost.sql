\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id,email) VALUES ('00000000-0000-0000-0000-0000000009a1','cost-a@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000009a1',true);
SELECT trellis.ensure_account();
DO $$
DECLARE chat uuid; run uuid; call uuid; r jsonb;
    cap text:='ClearCostCapabilityWithEnoughRandomCharacters123';
BEGIN
    r:=trellis.mutate('create_session','{}'::jsonb); chat:=(r->>'id')::uuid;
    r:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','turn','client_request_id','request','content','Hello','model_id','openai:gpt-5.5'),cap);
    run:=(r->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,'next_status','running','event_type','run.started'),cap);
    r:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
    call:=(r->>'id')::uuid;
    r:=trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','streaming','estimated_cost',0.01),cap);
    r:=trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','streaming'),cap);
    IF r->>'estimated_cost' IS DISTINCT FROM '0.01' THEN RAISE EXCEPTION 'status update lost cost'; END IF;
    BEGIN
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','streaming','clear_estimated_cost',true),'ForeignCapabilityWithEnoughRandomCharacters123');
        RAISE EXCEPTION 'foreign lease cleared cost';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    r:=trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','streaming','clear_estimated_cost',true),cap);
    IF r->>'estimated_cost' IS NOT NULL OR (SELECT estimated_cost FROM trellis.model_calls WHERE id=call) IS NOT NULL THEN
        RAISE EXCEPTION 'unknown cost retained a stale estimate'; END IF;
    r:=trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','completed'),cap);
    IF r->>'estimated_cost' IS NOT NULL THEN RAISE EXCEPTION 'completed call restored stale cost'; END IF;
END; $$;
RESET ROLE;
DO $$ BEGIN
    IF has_function_privilege('authenticated','trellis_private.mutate_before_unknown_usage_cost(text,jsonb,text)','EXECUTE') THEN
        RAISE EXCEPTION 'account bypassed cost wrapper'; END IF;
END; $$;
ROLLBACK;
