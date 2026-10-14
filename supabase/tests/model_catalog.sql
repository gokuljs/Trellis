\set ON_ERROR_STOP on
BEGIN;
DO $check$
DECLARE v_document jsonb;
BEGIN
    SELECT trusted_metadata INTO v_document FROM trellis.models WHERE id='openai:gpt-5.5';
    IF v_document->>'context_window_tokens' <> '1050000'
       OR NOT EXISTS(SELECT 1 FROM trellis.models WHERE id='openai:gpt-5.5'
           AND enabled AND metadata_status='ready' AND metadata_verified_at IS NOT NULL) THEN
        RAISE EXCEPTION 'existing model lacks verified limits and readiness';
    END IF;
    BEGIN
        INSERT INTO trellis.models(id,provider_id,provider_name,adapter_kind,upstream_model_id,name,
            requires_api_key,supports_streaming,supports_tools,enabled,catalog_order,
            metadata_status,trusted_metadata,metadata_verified_at)
        VALUES('openai:arbitrary','openai','OpenAI','openai','arbitrary','Unknown',true,true,true,true,10000,
            'ready',v_document,now());
        RAISE EXCEPTION 'arbitrary upstream accepted as ready';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    INSERT INTO trellis.models(id,provider_id,provider_name,adapter_kind,upstream_model_id,name,
        requires_api_key,supports_streaming,supports_tools,enabled,catalog_order)
    VALUES('openai:pending','openai','OpenAI','openai','pending','Pending',true,false,false,false,10000);
END;
$check$;
INSERT INTO auth.users(id,email) VALUES
('00000000-0000-0000-0000-0000000000e1','catalog-a@example.test'),
('00000000-0000-0000-0000-0000000000e2','catalog-b@example.test');
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000000e1',true);
SELECT trellis.ensure_account();
DO $check$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM trellis.models WHERE id='openai:pending' AND NOT enabled) THEN
        RAISE EXCEPTION 'pending catalog is invisible to authenticated settings';
    END IF;
    IF trellis.mutate('set_selected_model','{"model_id":"openai:pending"}'::jsonb) <> 'false'::jsonb THEN
        RAISE EXCEPTION 'pending model selected';
    END IF;
    BEGIN
        UPDATE trellis.models SET trusted_metadata='{}'::jsonb WHERE id='openai:gpt-5.5';
        RAISE EXCEPTION 'authenticated user can overwrite trusted model metadata';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        SELECT document FROM trellis_private.model_profiles LIMIT 1;
        RAISE EXCEPTION 'authenticated user can access private metadata write source';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$check$;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000000e2',true);
SELECT trellis.ensure_account();
DO $check$
BEGIN
    IF EXISTS(SELECT 1 FROM trellis.user_settings WHERE user_id='00000000-0000-0000-0000-0000000000e1') THEN
        RAISE EXCEPTION 'catalog migration weakened settings ownership';
    END IF;
END;
$check$;
RESET ROLE;
ROLLBACK;
