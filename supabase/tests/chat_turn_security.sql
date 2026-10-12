\set ON_ERROR_STOP on

INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000f1', 'active@example.test'),
    ('00000000-0000-0000-0000-0000000000f8', 'admission@example.test');

SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000f1', false);
SELECT trellis.ensure_account();

DO $check$
DECLARE
    v_chat uuid;
    v_run uuid;
    v_result jsonb;
    v_cap text := 'ActiveRunCapabilityWithThirtyTwoBytes123456';
BEGIN
    v_chat := (trellis.mutate('create_session', '{"workspace_path":null}'::jsonb)->>'id')::uuid;
    v_result := trellis.mutate('create_run', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'active-turn',
        'client_request_id', 'active-request', 'content', 'Still executing',
        'model_id', 'openai:gpt-5.5', 'budget_preset', 'conservative'), v_cap);
    v_run := (v_result->'run'->>'id')::uuid;

    BEGIN
        PERFORM trellis.mutate('add_assistant_message',
            pg_catalog.jsonb_build_object('session_id', v_chat,
                'turn_id', 'active-turn', 'content', 'Forged completion',
                'provider', 'openai', 'model', 'openai:gpt-5.5'));
        RAISE EXCEPTION 'classic assistant write bypassed the active run';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
    END;
    BEGIN
        PERFORM trellis.mutate('add_user_message',
            pg_catalog.jsonb_build_object('session_id', v_chat,
                'turn_id', 'foreign-turn', 'content', 'Forged input'));
        RAISE EXCEPTION 'classic user write bypassed the active run';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
    END;
    IF (SELECT count(*) FROM trellis.run_events WHERE run_id = v_run) <> 1 THEN
        RAISE EXCEPTION 'rejected classic writes changed active run events';
    END IF;
END
$check$;

SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000f8', false);
SELECT trellis.ensure_account();

DO $check$
DECLARE
    v_chat uuid;
    v_run uuid;
    v_cap text := 'AdmissionRunCapabilityWithThirtyTwoBytes123';
    v_claim_cap text := 'ClassicTurnCapabilityWithThirtyTwoBytes123';
    v_next_claim_cap text := 'NextClassicCapabilityWithThirtyTwoBytes123';
    v_foreign_cap text := 'OtherDeviceCapabilityWithThirtyTwoBytes123';
BEGIN
    v_chat := (trellis.mutate('create_session', '{"workspace_path":null}'::jsonb)->>'id')::uuid;
    IF trellis.mutate('claim_turn', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'classic-turn'), v_claim_cap) <> 'true'::jsonb THEN
        RAISE EXCEPTION 'classic turn was not claimed';
    END IF;
    PERFORM trellis.mutate('release_turn', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'classic-turn'), v_foreign_cap);
    BEGIN
        PERFORM trellis.mutate('create_run', pg_catalog.jsonb_build_object(
            'session_id', v_chat, 'turn_id', 'agent-turn',
            'client_request_id', 'agent-request', 'content', 'Agent input',
            'model_id', 'openai:gpt-5.5', 'budget_preset', 'conservative'), v_cap);
        RAISE EXCEPTION 'run entered while classic turn was claimed';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
    END;
    BEGIN
        PERFORM trellis.mutate('add_assistant_message',
            pg_catalog.jsonb_build_object('session_id', v_chat,
                'turn_id', 'classic-turn', 'content', 'Forged assistant',
                'provider', 'openai', 'model', 'openai:gpt-5.5'), v_foreign_cap);
        RAISE EXCEPTION 'foreign capability wrote classic assistant';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        PERFORM trellis.mutate('add_user_message',
            pg_catalog.jsonb_build_object('session_id', v_chat,
                'turn_id', 'classic-turn', 'content', 'Forged input'));
        RAISE EXCEPTION 'missing capability wrote classic user message';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    PERFORM trellis.mutate('add_user_message', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'classic-turn', 'content', 'Actual input'), v_claim_cap);
    PERFORM trellis.mutate('add_assistant_message', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'classic-turn', 'content', 'Actual output',
        'provider', 'openai', 'model', 'openai:gpt-5.5'), v_claim_cap);
    PERFORM trellis.mutate('release_turn', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'classic-turn'), v_claim_cap);
    v_run := (trellis.mutate('create_run', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'agent-turn',
        'client_request_id', 'agent-request', 'content', 'Agent input',
        'model_id', 'openai:gpt-5.5', 'budget_preset', 'conservative'),
        v_cap)->'run'->>'id')::uuid;
    BEGIN
        PERFORM trellis.mutate('claim_turn', pg_catalog.jsonb_build_object(
            'session_id', v_chat, 'turn_id', 'classic-turn'), v_next_claim_cap);
        RAISE EXCEPTION 'classic turn entered while run was active';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
    END;
    PERFORM trellis.mutate('request_run_cancellation',
        pg_catalog.jsonb_build_object('run_id', v_run), v_cap);
    IF trellis.mutate('claim_turn', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'classic-turn'), v_next_claim_cap)
        <> 'true'::jsonb THEN
        RAISE EXCEPTION 'classic turn did not enter after run finished';
    END IF;
    PERFORM trellis.mutate('release_turn', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'classic-turn'), v_next_claim_cap);
END
$check$;

RESET ROLE;
