\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id,email) VALUES
    ('00000000-0000-0000-0000-0000000011a1','reviewed-dispatch-a@example.test'),
    ('00000000-0000-0000-0000-0000000011b1','reviewed-dispatch-b@example.test');
SET LOCAL ROLE trellis_catalog_sync;
SELECT trellis_private.sync_model_catalog('openai','["unreviewed-discovery"]'::jsonb);
SELECT trellis_private.sync_model_catalog('anthropic','[]'::jsonb);
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000011a1',true);
SELECT trellis.ensure_account();
DO $$
DECLARE result jsonb;
BEGIN
    result:=trellis.mutate('set_selected_model',jsonb_build_object('model_id','openai:gpt-6.1-sol'));
    IF result IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION 'reviewed model rejected after an unavailable provider snapshot'; END IF;
    IF NOT EXISTS(SELECT 1 FROM trellis.models WHERE id='openai:gpt-6.1-sol' AND enabled
        AND metadata_status='ready' AND availability='unavailable' AND availability_checked_at IS NOT NULL) THEN
        RAISE EXCEPTION 'reviewed eligibility or truthful unavailable observation was lost'; END IF;
    PERFORM trellis.mutate('set_selected_provider',jsonb_build_object('provider','anthropic'));
    IF NOT EXISTS(SELECT 1 FROM trellis.user_settings WHERE user_id=auth.uid()
        AND selected_provider='anthropic' AND selected_model_id='anthropic:claude-sonnet-5') THEN
        RAISE EXCEPTION 'provider selection rejected unavailable reviewed models'; END IF;
    IF trellis.mutate('set_selected_model',jsonb_build_object('model_id','openai:unreviewed-discovery'))
        IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION 'unreviewed discovery became selectable'; END IF;
    IF trellis.mutate('set_selected_model',jsonb_build_object('model_id','openai:unknown'))
        IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION 'unknown model became selectable'; END IF;
END; $$;
DO $$
DECLARE chat uuid; run uuid; result jsonb;
    cap text:='ReviewedDispatchCapabilityWithEnoughRandomCharacters123';
BEGIN
    result:=trellis.mutate('create_session','{}'::jsonb); chat:=(result->>'id')::uuid;
    result:=trellis.mutate('set_session_model',jsonb_build_object('session_id',chat,
        'model_id','openai:gpt-6.1-sol'));
    IF result->>'model_id' IS DISTINCT FROM 'openai:gpt-6.1-sol' THEN
        RAISE EXCEPTION 'session model rejected unavailable reviewed model'; END IF;
    BEGIN
        PERFORM trellis.mutate('set_session_model',jsonb_build_object('session_id',chat,
            'model_id','openai:unreviewed-discovery'));
        RAISE EXCEPTION 'session accepted unreviewed discovery';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','unknown',
            'client_request_id','unknown','content','Hello','model_id','openai:unreviewed-discovery'),cap);
        RAISE EXCEPTION 'run accepted unreviewed discovery';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','unknown',
            'client_request_id','unknown','content','Hello','model_id','openai:unknown'),cap);
        RAISE EXCEPTION 'run accepted unknown model';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    result:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','reviewed',
        'client_request_id','reviewed','content','Hello','model_id','openai:gpt-6.1-sol'),cap);
    run:=(result->'run'->>'id')::uuid;
    IF result->'run'->>'model_id' IS DISTINCT FROM 'openai:gpt-6.1-sol'
       OR result->'run'->>'provider_id' IS DISTINCT FROM 'openai'
       OR result->'run'->>'upstream_model_id' IS DISTINCT FROM 'gpt-6.1-sol' THEN
        RAISE EXCEPTION 'run changed reviewed provider identity'; END IF;
    BEGIN
        PERFORM trellis.mutate('set_session_model',jsonb_build_object('session_id',chat,
            'model_id','anthropic:claude-sonnet-5'));
        RAISE EXCEPTION 'reviewed model bypassed active-run session guard';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','running','event_type','run.started'),cap);
    PERFORM set_config('trellis.test_reviewed_run',run::text,true);
    PERFORM set_config('trellis.test_reviewed_chat',chat::text,true);
END; $$;
RESET ROLE;
-- Availability can change again between run creation and model-call allocation.
SET LOCAL ROLE trellis_catalog_sync;
SELECT trellis_private.sync_model_catalog('openai','[]'::jsonb);
RESET ROLE;
SET LOCAL ROLE authenticated;
DO $$
DECLARE run uuid:=current_setting('trellis.test_reviewed_run')::uuid; result jsonb;
    cap text:='ReviewedDispatchCapabilityWithEnoughRandomCharacters123';
BEGIN
    BEGIN
        PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
            'model_id','openai:gpt-6.1-sol','request_snapshot','{}'::jsonb),
            'WrongReviewedCapabilityWithEnoughRandomCharacters123');
        RAISE EXCEPTION 'reviewed model bypassed run capability';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
            'model_id','anthropic:claude-sonnet-5','request_snapshot','{}'::jsonb),cap);
        RAISE EXCEPTION 'reviewed model bypassed run model identity';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
            'model_id','openai:unknown','request_snapshot','{}'::jsonb),cap);
        RAISE EXCEPTION 'model call accepted unknown model';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    result:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
        'model_id','openai:gpt-6.1-sol','request_snapshot','{}'::jsonb),cap);
    IF result->>'model_id' IS DISTINCT FROM 'openai:gpt-6.1-sol'
       OR result->>'status' IS DISTINCT FROM 'pending' THEN
        RAISE EXCEPTION 'unavailable reviewed model could not allocate a model call'; END IF;
    PERFORM set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000011b1',true);
    PERFORM trellis.ensure_account();
    result:=trellis.mutate('set_session_model',jsonb_build_object(
        'session_id',current_setting('trellis.test_reviewed_chat')::uuid,
        'model_id','anthropic:claude-sonnet-5'));
    IF result IS NOT NULL THEN RAISE EXCEPTION 'reviewed model bypassed session ownership'; END IF;
    BEGIN
        PERFORM trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',2,
            'model_id','openai:gpt-6.1-sol','request_snapshot','{}'::jsonb),cap);
        RAISE EXCEPTION 'reviewed model bypassed run ownership';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000011a1',true);
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','failed','event_type','run.failed'),cap);
END; $$;
RESET ROLE;
DO $$
BEGIN
    IF EXISTS(SELECT 1 FROM trellis.models WHERE metadata_status='ready' AND NOT enabled) THEN
        RAISE EXCEPTION 'migration or sync left a reviewed model disabled'; END IF;
    BEGIN
        INSERT INTO trellis.models(id,provider_id,provider_name,adapter_kind,upstream_model_id,name,
            requires_api_key,supports_streaming,supports_tools,enabled,catalog_order)
        VALUES('openai:unreviewed-enabled','openai','OpenAI','openai','unreviewed-enabled',
            'Unreviewed',true,false,false,true,99999);
        RAISE EXCEPTION 'pending metadata became runtime eligible';
    EXCEPTION WHEN check_violation THEN NULL; END;
    IF has_function_privilege('authenticated','trellis_private.sync_model_catalog(text,jsonb)','EXECUTE')
       OR has_function_privilege('anon','trellis_private.sync_model_catalog(text,jsonb)','EXECUTE')
       OR has_function_privilege('authenticated','trellis_private.mutate_before_context_checkpoints(text,jsonb,text)','EXECUTE')
       OR has_function_privilege('trellis_catalog_sync','trellis_private.mutate_before_context_checkpoints(text,jsonb,text)','EXECUTE') THEN
        RAISE EXCEPTION 'reviewed dispatch exposed a private mutation helper'; END IF;
END; $$;
ROLLBACK;
