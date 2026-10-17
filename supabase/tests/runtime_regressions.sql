\set ON_ERROR_STOP on

INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000f6', 'frank@example.test');
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000f6', false);
SELECT trellis.ensure_account();

DO $check$
DECLARE
    chat_id uuid;
    v_run_id uuid;
    model_call_id uuid;
    tool_call_id uuid;
    result jsonb;
    event_count integer;
    cap text := 'RuntimeRegressionCapabilityWithThirtyTwoCharacters123';
    tool jsonb := pg_catalog.jsonb_build_object(
        'id', 'provider-call-f6', 'name', 'run_command',
        'arguments', '{"command":"echo ok"}'::jsonb);
BEGIN
    result := trellis.mutate('create_session', '{"workspace_path":null}'::jsonb);
    chat_id := (result->>'id')::uuid;
    result := trellis.mutate('create_run', pg_catalog.jsonb_build_object(
        'session_id', chat_id, 'turn_id', 'turn-f6',
        'client_request_id', 'request-f6', 'content', 'Run a test',
        'model_id', 'openai:gpt-5.5', 'budget_preset', 'conservative'), cap);
    v_run_id := (result->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record', pg_catalog.jsonb_build_object(
        'run_id', v_run_id, 'next_status', 'running',
        'event_type', 'run.started', 'data', '{}'::jsonb), cap);
    result := trellis.mutate('create_model_call', pg_catalog.jsonb_build_object(
        'run_id', v_run_id, 'step_index', 1, 'model_id', 'openai:gpt-5.5',
        'request_snapshot', '{}'::jsonb), cap);
    model_call_id := (result->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call', pg_catalog.jsonb_build_object(
        'call_id', model_call_id, 'next_status', 'completed',
        'response_snapshot', '{}'::jsonb), cap);
    SELECT count(*) INTO event_count FROM trellis.run_events WHERE run_events.run_id = v_run_id;

    BEGIN
        PERFORM trellis.mutate('record_assistant_message',
            pg_catalog.jsonb_build_object('run_id', v_run_id,
                'model_call_id', model_call_id,
                'message', pg_catalog.jsonb_build_object(
                    'role', 'assistant', 'content', '',
                    'tool_calls', pg_catalog.jsonb_build_array(tool, tool),
                    'continuation_items', '[]'::jsonb)), cap);
        RAISE EXCEPTION 'duplicate provider call IDs accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
    IF EXISTS (SELECT 1 FROM trellis.run_messages WHERE run_messages.run_id = v_run_id)
       OR EXISTS (SELECT 1 FROM trellis.tool_calls WHERE tool_calls.run_id = v_run_id)
       OR (SELECT count(*) FROM trellis.run_events WHERE run_events.run_id = v_run_id)
          <> event_count THEN
        RAISE EXCEPTION 'failed assistant message left a partial transcript';
    END IF;

    result := trellis.mutate('record_assistant_message',
        pg_catalog.jsonb_build_object('run_id', v_run_id,
            'model_call_id', model_call_id,
            'message', pg_catalog.jsonb_build_object(
                'role', 'assistant', 'content', '',
                'tool_calls', pg_catalog.jsonb_build_array(tool),
                'continuation_items', '[]'::jsonb)), cap);
    tool_call_id := (result->'tool_calls'->0->>'id')::uuid;
    PERFORM trellis.mutate('request_tool_approval',
        pg_catalog.jsonb_build_object('run_id', v_run_id,
            'tool_call_id', tool_call_id, 'preview', '{}'::jsonb), cap);
    result := trellis.mutate('record_tool_approval_decision',
        pg_catalog.jsonb_build_object('run_id', v_run_id,
            'tool_call_id', tool_call_id, 'decision', 'denied'), cap);
    IF result->'tool_call'->>'approval_decision' IS DISTINCT FROM 'denied'
       OR result->'event' IS NULL
       OR result->'event' = 'null'::jsonb THEN
        RAISE EXCEPTION 'denied approval decision not saved';
    END IF;
    SELECT count(*) INTO event_count FROM trellis.run_events WHERE run_events.run_id = v_run_id;
    result := trellis.mutate('record_tool_approval_decision',
        pg_catalog.jsonb_build_object('run_id', v_run_id,
            'tool_call_id', tool_call_id, 'decision', 'denied'), cap);
    IF result->'event' IS DISTINCT FROM 'null'::jsonb
       OR (SELECT count(*) FROM trellis.run_events WHERE run_events.run_id = v_run_id)
          <> event_count THEN
        RAISE EXCEPTION 'repeat approval decision emitted another event';
    END IF;
    BEGIN
        PERFORM trellis.mutate('record_tool_approval_decision',
            pg_catalog.jsonb_build_object('run_id', v_run_id,
                'tool_call_id', tool_call_id, 'decision', 'approved'), cap);
        RAISE EXCEPTION 'opposite approval decision accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
END
$check$;

RESET ROLE;
