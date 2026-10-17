\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id,email) VALUES('00000000-0000-0000-0000-0000000002c1','chat-models@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000002c1',true);
SELECT trellis.ensure_account();
DO $$
DECLARE first_chat uuid; second_chat uuid; result jsonb; run uuid;
    cap text := 'ChatModelCapabilityLongEnoughForLeaseProtection12345';
BEGIN
    result:=trellis.mutate('create_session','{}'::jsonb);
    first_chat:=(result->>'id')::uuid;
    IF result->>'model_id' IS DISTINCT FROM 'openai:gpt-5.5' THEN RAISE EXCEPTION 'chat default missing'; END IF;
    PERFORM trellis.mutate('set_selected_model',jsonb_build_object('model_id','anthropic:claude-sonnet-5'));
    result:=trellis.mutate('create_session','{}'::jsonb);
    second_chat:=(result->>'id')::uuid;
    IF result->>'model_id' IS DISTINCT FROM 'anthropic:claude-sonnet-5' THEN RAISE EXCEPTION 'new chat default missing'; END IF;
    IF (SELECT model_id FROM trellis.chats WHERE id=first_chat) IS DISTINCT FROM 'openai:gpt-5.5' THEN
        RAISE EXCEPTION 'global default changed existing chat'; END IF;
    result:=trellis.mutate('create_run',jsonb_build_object('session_id',first_chat,'turn_id','models',
        'client_request_id','models','content','Hi','model_id','openai:gpt-5.5'),cap);
    run:=(result->'run'->>'id')::uuid;
    BEGIN
        PERFORM trellis.mutate('set_session_model',jsonb_build_object('session_id',first_chat,'model_id','anthropic:claude-sonnet-5'));
        RAISE EXCEPTION 'active model changed';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
    PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',run),cap);
    PERFORM trellis.mutate('set_session_model',jsonb_build_object('session_id',first_chat,'model_id','anthropic:claude-sonnet-5'));
    BEGIN
        PERFORM trellis.mutate('set_session_model',jsonb_build_object('session_id',first_chat,'model_id','unknown:pending'));
        RAISE EXCEPTION 'pending model accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    PERFORM set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000002c2',true);
    result:=trellis.mutate('set_session_model',jsonb_build_object('session_id',first_chat,'model_id','openai:gpt-5.5'));
    IF result IS NOT NULL THEN RAISE EXCEPTION 'foreign chat changed'; END IF;
END;
$$;
RESET ROLE;
-- A legacy preference can refer to a model that is now pending verification.
INSERT INTO auth.users(id,email) VALUES('00000000-0000-0000-0000-0000000002c3','legacy-chat-model@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000002c3',true);
SELECT trellis.ensure_account();
SELECT trellis.mutate('create_session','{}'::jsonb);
RESET ROLE;
INSERT INTO trellis.models(id,provider_id,provider_name,adapter_kind,upstream_model_id,name,
    requires_api_key,supports_streaming,supports_tools,enabled,catalog_order)
VALUES('openai:legacy-pending','openai','OpenAI','openai','legacy-pending','Legacy pending',true,false,false,false,999);
UPDATE trellis.chats SET model_id='openai:legacy-pending' WHERE user_id='00000000-0000-0000-0000-0000000002c3';
SET LOCAL ROLE trellis_catalog_sync;
SELECT trellis_private.sync_model_catalog('openai','["gpt-5.5"]'::jsonb);
RESET ROLE;
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM trellis.chats WHERE user_id='00000000-0000-0000-0000-0000000002c3'
        AND model_id='openai:legacy-pending') THEN RAISE EXCEPTION 'catalog sync removed a saved chat preference'; END IF;
END; $$;
ROLLBACK;
