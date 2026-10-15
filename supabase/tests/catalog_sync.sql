\set ON_ERROR_STOP on
BEGIN;
DO $check$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trellis_catalog_sync' AND rolcanlogin
        AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication
        AND NOT rolbypassrls AND NOT rolinherit) THEN
        RAISE EXCEPTION 'catalog operator role is missing or privileged';
    END IF;
    IF has_function_privilege('authenticated','trellis_private.sync_model_catalog(text,jsonb)','EXECUTE')
       OR has_function_privilege('anon','trellis_private.sync_model_catalog(text,jsonb)','EXECUTE') THEN
        RAISE EXCEPTION 'app roles can sync catalog';
    END IF;
END;
$check$;
SET LOCAL ROLE trellis_catalog_sync;
DO $check$
BEGIN
    BEGIN
        SELECT id FROM trellis.models LIMIT 1;
        RAISE EXCEPTION 'catalog role can read application models';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        UPDATE trellis.profiles SET display_name='operator overwrite';
        RAISE EXCEPTION 'catalog role can write account data';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        SELECT document FROM trellis_private.model_profiles LIMIT 1;
        RAISE EXCEPTION 'catalog role can read/write trusted metadata source';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        PERFORM trellis.mutate('create_session','{}'::jsonb);
        RAISE EXCEPTION 'catalog role can invoke account mutation';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        PERFORM trellis_private.sync_model_catalog('other','[]'::jsonb);
        RAISE EXCEPTION 'unknown provider accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM trellis_private.sync_model_catalog('openai','["bad''$(secret)"]'::jsonb);
        RAISE EXCEPTION 'unsafe upstream identifier accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
END;
$check$;
SELECT trellis_private.sync_model_catalog('openai','["gpt-6.1-sol","future-unverified"]'::jsonb);
RESET ROLE;
DO $check$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM trellis.models WHERE id='openai:gpt-6.1-sol' AND enabled
        AND metadata_status='ready' AND availability='available' AND availability_checked_at IS NOT NULL) THEN
        RAISE EXCEPTION 'known verified available model did not become ready';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM trellis.models WHERE id='openai:gpt-5.5' AND enabled
        AND metadata_status='ready' AND availability='unavailable') THEN
        RAISE EXCEPTION 'successful snapshot removed reviewed eligibility or lost unavailable observation';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM trellis.models WHERE id='openai:future-unverified' AND NOT enabled
        AND metadata_status='pending' AND trusted_metadata IS NULL AND metadata_verified_at IS NULL
        AND availability='available' AND NOT supports_streaming AND NOT supports_tools) THEN
        RAISE EXCEPTION 'unknown discovery borrowed reviewed readiness metadata';
    END IF;
END;
$check$;
SET LOCAL ROLE trellis_catalog_sync;
SELECT trellis_private.sync_model_catalog('openai','["gpt-5.5","gpt-6.1-sol"]'::jsonb);
RESET ROLE;
DO $check$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM trellis.models WHERE id='openai:gpt-5.5' AND enabled
        AND availability='available' AND trusted_metadata->>'context_window_tokens'='1050000') THEN
        RAISE EXCEPTION 'catalog did not recover available verified model';
    END IF;
    IF EXISTS(SELECT 1 FROM trellis.models WHERE id='openai:future-unverified') THEN
        RAISE EXCEPTION 'unknown unavailable discovery was retained without an account reference';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM trellis.models WHERE id='anthropic:claude-sonnet-5' AND enabled) THEN
        RAISE EXCEPTION 'OpenAI refresh changed another provider';
    END IF;
END;
$check$;
-- An empty but successful provider snapshot must not block account reads.
INSERT INTO auth.users(id,email) VALUES
('00000000-0000-0000-0000-0000000003a0','catalog-existing@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000003a0',true);
SELECT trellis.ensure_account();
RESET ROLE;
SET LOCAL ROLE trellis_catalog_sync;
SELECT trellis_private.sync_model_catalog('openai','[]'::jsonb);
SELECT trellis_private.sync_model_catalog('anthropic','[]'::jsonb);
RESET ROLE;
INSERT INTO auth.users(id,email) VALUES
('00000000-0000-0000-0000-0000000003a1','catalog-empty@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000003a0',true);
SELECT trellis.ensure_account();
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000003a1',true);
SELECT trellis.ensure_account();
DO $check$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM trellis.user_settings s JOIN trellis.models m
        ON m.id=s.selected_model_id WHERE s.user_id=auth.uid() AND m.metadata_status='ready') THEN
        RAISE EXCEPTION 'empty-availability account default selected a pending profile';
    END IF;
END;
$check$;
RESET ROLE;
ROLLBACK;
