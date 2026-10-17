\set ON_ERROR_STOP on

INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000c7', 'run-contract@example.test');
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub',
    '00000000-0000-0000-0000-0000000000c7', false);
SELECT trellis.ensure_account();

DO $check$
DECLARE
    v_chat_id uuid;
    first_run_id uuid;
    input_message_id uuid;
    result jsonb;
    payload jsonb;
    cap text := 'CreateRunRegressionCapabilityWithThirtyTwoBytes123';
    retry_cap text := 'CreateRunRetryCapabilityWithThirtyTwoBytes123';
BEGIN
    v_chat_id := (trellis.mutate('create_session',
        '{"workspace_path":null}'::jsonb)->>'id')::uuid;
    payload := pg_catalog.jsonb_build_object(
        'session_id', v_chat_id, 'turn_id', 'turn-c7',
        'client_request_id', 'request-c7',
        'content', '  hello   from  Trellis  ',
        'model_id', 'openai:gpt-5.5', 'budget_preset', 'longer');
    result := trellis.mutate('create_run', payload, cap);
    first_run_id := (result->'run'->>'id')::uuid;
    input_message_id := (result->'run'->>'input_message_id')::uuid;
    IF first_run_id IS NULL OR result->>'lease_acquired' IS DISTINCT FROM 'true'
       OR result->'run'->>'budget_preset' IS DISTINCT FROM 'longer'
       OR (result->'run'->>'max_model_calls')::integer IS DISTINCT FROM 15
       OR (result->'run'->>'max_tool_calls')::integer IS DISTINCT FROM 30
       OR (result->'run'->>'max_total_tokens')::integer IS DISTINCT FROM 200000
       OR (result->'run'->>'max_cost_usd')::numeric IS DISTINCT FROM 5 THEN
        RAISE EXCEPTION 'run creation did not save selected budget';
    END IF;
    IF (SELECT title FROM trellis.chats WHERE id = v_chat_id)
          IS DISTINCT FROM 'hello from Trellis'
       OR (SELECT content FROM trellis.messages WHERE id = input_message_id)
          IS DISTINCT FROM 'hello   from  Trellis' THEN
        RAISE EXCEPTION 'first run did not normalize the visible chat title';
    END IF;

    -- A duplicate request is observable by another backend, but it grants no lease.
    result := trellis.mutate('create_run', payload ||
        '{"content":"hello   from  Trellis"}'::jsonb,
        'OtherBackendCapabilityWithThirtyTwoBytes123');
    IF (result->'run'->>'id')::uuid IS DISTINCT FROM first_run_id
       OR result->>'lease_acquired' IS DISTINCT FROM 'false'
       OR result->'run' ? 'lease_token' THEN
        RAISE EXCEPTION 'duplicate request changed run identity or exposed a lease';
    END IF;
    BEGIN
        PERFORM trellis.mutate('create_run', payload ||
            '{"budget_preset":"conservative"}'::jsonb, cap);
        RAISE EXCEPTION 'duplicate request accepted a different budget';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM trellis.mutate('create_run', payload ||
            '{"model_id":"anthropic:claude-sonnet-5"}'::jsonb, cap);
        RAISE EXCEPTION 'duplicate request accepted a different model';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM trellis.mutate('create_run', payload ||
            '{"content":"different input"}'::jsonb, cap);
        RAISE EXCEPTION 'duplicate request accepted different input';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM trellis.mutate('create_run',
            pg_catalog.jsonb_build_object(
                'session_id', v_chat_id, 'turn_id', 'other-turn',
                'client_request_id', 'other-request', 'content', 'Second question',
                'model_id', 'openai:gpt-5.5'),
            'OtherRunCapabilityWithThirtyTwoBytes123');
        RAISE EXCEPTION 'a second active run entered the chat';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    IF (SELECT count(*) FROM trellis.runs WHERE runs.chat_id = v_chat_id) <> 1
       OR (SELECT count(*) FROM trellis.messages WHERE messages.chat_id = v_chat_id) <> 1
       OR (SELECT count(*) FROM trellis.run_events WHERE run_id = first_run_id) <> 1 THEN
        RAISE EXCEPTION 'rejected run request left partial rows or events';
    END IF;

    PERFORM trellis.mutate('transition_run_record',
        pg_catalog.jsonb_build_object(
            'run_id', first_run_id, 'next_status', 'failed',
            'event_type', 'run.failed', 'data', '{"code":"provider_failed"}'::jsonb),
        cap);
    result := trellis.mutate('create_run', payload ||
        '{"client_request_id":"retry-c7"}'::jsonb, retry_cap);
    IF (result->'run'->>'id')::uuid IS NULL
       OR (result->'run'->>'id')::uuid = first_run_id
       OR (result->'run'->>'retry_of')::uuid IS DISTINCT FROM first_run_id
       OR (result->'run'->>'input_message_id')::uuid IS DISTINCT FROM input_message_id
       OR result->>'lease_acquired' IS DISTINCT FROM 'true' THEN
        RAISE EXCEPTION 'failed turn did not create a new linked attempt';
    END IF;
    IF (SELECT count(*) FROM trellis.runs WHERE runs.chat_id = v_chat_id) <> 2
       OR (SELECT count(*) FROM trellis.messages WHERE messages.chat_id = v_chat_id) <> 1
       OR (SELECT count(*) FROM trellis.run_events
           WHERE run_id = (result->'run'->>'id')::uuid) <> 1 THEN
        RAISE EXCEPTION 'retry duplicated the visible user message or queued event';
    END IF;
END
$check$;

RESET ROLE;
