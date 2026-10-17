\set ON_ERROR_STOP on

DO $check$
BEGIN
    IF has_function_privilege('anon', 'trellis.mutate(text,jsonb,text)', 'EXECUTE') THEN
        RAISE EXCEPTION 'anon can call mutation RPC';
    END IF;
END
$check$;

INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000a1', 'alice@example.test'),
    ('00000000-0000-0000-0000-0000000000b2', 'bob@example.test');

SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000a1', false);
SELECT trellis.ensure_account();
SELECT trellis.mutate('create_session', '{"workspace_path":null}'::jsonb);

DO $check$
DECLARE
    chat_id uuid;
    run_id uuid;
    result jsonb;
BEGIN
    SELECT id INTO chat_id FROM trellis.chats LIMIT 1;
    IF chat_id IS NULL THEN
        RAISE EXCEPTION 'create_session did not create a chat';
    END IF;

    result := trellis.mutate('create_run', pg_catalog.jsonb_build_object(
        'session_id', chat_id,
        'turn_id', 'turn-a',
        'client_request_id', 'request-a',
        'content', 'Hello',
        'model_id', 'openai:gpt-5.5',
        'budget_preset', 'conservative'
    ), 'AliceCapabilityWithAtLeastThirtyTwoCharacters123');
    run_id := (result->'run'->>'id')::uuid;
    IF run_id IS NULL OR result->>'lease_acquired' <> 'true' THEN
        RAISE EXCEPTION 'create_run did not acquire lease';
    END IF;
    IF result->'run' ? 'lease_token' THEN
        RAISE EXCEPTION 'run capability digest returned to caller';
    END IF;

    BEGIN
        PERFORM trellis.mutate('request_run_cancellation',
            pg_catalog.jsonb_build_object('run_id', run_id), null);
        RAISE EXCEPTION 'missing capability accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;

    BEGIN
        PERFORM trellis.mutate('request_run_cancellation',
            pg_catalog.jsonb_build_object('run_id', run_id),
            'WrongCapabilityWithAtLeastThirtyTwoCharacters123');
        RAISE EXCEPTION 'wrong capability accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;

    PERFORM set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000b2', false);
    IF EXISTS (SELECT 1 FROM trellis.runs WHERE id=run_id) THEN
        RAISE EXCEPTION 'Bob can read Alice run';
    END IF;
    BEGIN
        PERFORM trellis.mutate('request_run_cancellation',
            pg_catalog.jsonb_build_object('run_id', run_id),
            'AliceCapabilityWithAtLeastThirtyTwoCharacters123');
        RAISE EXCEPTION 'Bob can mutate Alice run';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;

    PERFORM set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000a1', false);
    result := trellis.mutate('request_run_cancellation',
        pg_catalog.jsonb_build_object('run_id', run_id),
        'AliceCapabilityWithAtLeastThirtyTwoCharacters123');
    IF result->'run'->>'status' <> 'cancelled' THEN
        RAISE EXCEPTION 'owner with capability could not cancel queued run';
    END IF;
END
$check$;

DO $check$
BEGIN
    BEGIN
        PERFORM trellis.mutate('not_allowlisted', '{}'::jsonb);
        RAISE EXCEPTION 'unknown mutation action accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
END
$check$;

RESET ROLE;
