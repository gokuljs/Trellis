\set ON_ERROR_STOP on

INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000e5', 'eve@example.test');
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000e5', false);
SELECT trellis.ensure_account();

DO $check$
DECLARE
    chat_id uuid;
    run_id uuid;
    call_id uuid;
    tool_id uuid;
    result jsonb;
    seq_count integer;
    last_seq integer;
    cap text := 'RuntimeCapabilityWithAtLeastThirtyTwoCharacters123';
BEGIN
    result := trellis.mutate('create_session', '{"workspace_path":null}'::jsonb);
    chat_id := (result->>'id')::uuid;
    result := trellis.mutate('create_run', pg_catalog.jsonb_build_object(
        'session_id', chat_id, 'turn_id', 'turn-e',
        'client_request_id', 'request-e', 'content', 'Run a test',
        'model_id', 'openai:gpt-5.5', 'budget_preset', 'conservative'), cap);
    run_id := (result->'run'->>'id')::uuid;
    IF run_id IS NULL THEN RAISE EXCEPTION 'run was not created'; END IF;

    BEGIN
        PERFORM trellis.mutate('set_session_workspace',
            pg_catalog.jsonb_build_object('session_id', chat_id,
                                           'workspace_path', '/tmp/changed'));
        RAISE EXCEPTION 'workspace changed while run active';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
    END;

    result := trellis.mutate('transition_run_record', pg_catalog.jsonb_build_object(
        'run_id', run_id, 'next_status', 'running',
        'event_type', 'run.started', 'data', '{}'::jsonb), cap);
    IF result->>'event_type' <> 'run.started' THEN
        RAISE EXCEPTION 'run start event missing';
    END IF;

    result := trellis.mutate('create_model_call', pg_catalog.jsonb_build_object(
        'run_id', run_id, 'step_index', 1, 'model_id', 'openai:gpt-5.5',
        'request_snapshot', '{}'::jsonb), cap);
    call_id := (result->>'id')::uuid;
    result := trellis.mutate('update_model_call', pg_catalog.jsonb_build_object(
        'call_id', call_id, 'next_status', 'completed',
        'response_snapshot', '{}'::jsonb, 'input_tokens', 10,
        'output_tokens', 5), cap);
    IF result->>'status' <> 'completed' THEN
        RAISE EXCEPTION 'model call completion missing';
    END IF;

    result := trellis.mutate('record_assistant_message',
        pg_catalog.jsonb_build_object('run_id', run_id, 'model_call_id', call_id,
            'message', pg_catalog.jsonb_build_object(
                'role', 'assistant', 'content', '',
                'tool_calls', pg_catalog.jsonb_build_array(
                    pg_catalog.jsonb_build_object('id', 'provider-call-e',
                        'name', 'run_command',
                        'arguments', '{"command":"echo ok"}'::jsonb)),
                'continuation_items', '[]'::jsonb)), cap);
    tool_id := (result->'tool_calls'->0->>'id')::uuid;
    IF tool_id IS NULL OR pg_catalog.jsonb_array_length(result->'events') <> 2 THEN
        RAISE EXCEPTION 'assistant/tool event fanout incomplete';
    END IF;

    result := trellis.mutate('request_tool_approval',
        pg_catalog.jsonb_build_object('run_id', run_id, 'tool_call_id', tool_id,
                                      'preview', '{}'::jsonb), cap);
    IF result->'run'->>'status' <> 'waiting_for_approval' THEN
        RAISE EXCEPTION 'approval wait status missing';
    END IF;
    BEGIN
        PERFORM trellis.mutate('record_tool_approval_decision',
            pg_catalog.jsonb_build_object('run_id', run_id, 'tool_call_id', tool_id,
                'decision', 'approved'),
            'OtherDeviceCapabilityWithAtLeastThirtyTwoCharacters123');
        RAISE EXCEPTION 'other device approved tool';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    result := trellis.mutate('record_tool_approval_decision',
        pg_catalog.jsonb_build_object('run_id', run_id, 'tool_call_id', tool_id,
                                      'decision', 'approved'), cap);
    IF result->'tool_call'->>'approval_decision' <> 'approved' THEN
        RAISE EXCEPTION 'approval not saved';
    END IF;
    result := trellis.mutate('resume_approved_run',
        pg_catalog.jsonb_build_object('run_id', run_id), cap);
    IF result->'run'->>'status' <> 'running' THEN
        RAISE EXCEPTION 'approved run did not resume';
    END IF;
    result := trellis.mutate('record_tool_result',
        pg_catalog.jsonb_build_object('run_id', run_id, 'tool_call_id', tool_id,
            'content', 'ok', 'status', 'completed'), cap);
    IF result->'tool_call'->>'status' <> 'completed' THEN
        RAISE EXCEPTION 'tool result not saved';
    END IF;
    result := trellis.mutate('complete_run',
        pg_catalog.jsonb_build_object('run_id', run_id, 'content', 'Finished'), cap);
    IF pg_catalog.jsonb_array_length(result->'events') <> 2 THEN
        RAISE EXCEPTION 'run completion event fanout incomplete';
    END IF;
    SELECT count(*), max(sequence) INTO seq_count, last_seq
    FROM trellis.run_events e WHERE e.run_id = (result->'message'->>'run_id')::uuid;
    IF seq_count <> last_seq OR seq_count < 8 THEN
        RAISE EXCEPTION 'event sequence has a gap: count %, max %', seq_count, last_seq;
    END IF;
    IF (SELECT last_event_sequence FROM trellis.runs WHERE id=run_id) <> last_seq THEN
        RAISE EXCEPTION 'run event cursor disagrees with event table';
    END IF;
    result := trellis.mutate('set_session_workspace',
        pg_catalog.jsonb_build_object('session_id', chat_id,
                                       'workspace_path', '/tmp/changed'));
    IF result->>'workspace_path' <> '/tmp/changed' THEN
        RAISE EXCEPTION 'workspace update after run completion failed';
    END IF;
END
$check$;

DO $check$
DECLARE
    chat_id uuid;
    result jsonb;
    terminal_id uuid;
BEGIN
    result := trellis.mutate('create_session', '{"workspace_path":null}'::jsonb);
    chat_id := (result->>'id')::uuid;
    result := trellis.mutate('create_run', pg_catalog.jsonb_build_object(
        'session_id', chat_id, 'turn_id', 'turn-expiry',
        'client_request_id', 'request-expiry', 'content', 'Wait',
        'model_id', 'openai:gpt-5.5', 'budget_preset', 'conservative'),
        'ExpiryCapabilityWithAtLeastThirtyTwoCharacters123');
    result := trellis.mutate('recover_expired_run',
        pg_catalog.jsonb_build_object('run_id', result->'run'->>'id'));
    IF result->>'status' <> 'queued' OR
       (SELECT count(*) FROM trellis.run_events e
        WHERE e.run_id=(result->>'id')::uuid) <> 1 THEN
        RAISE EXCEPTION 'fresh lease was interrupted';
    END IF;

    result := trellis.mutate('create_session', '{"workspace_path":null}'::jsonb);
    chat_id := (result->>'id')::uuid;
    result := trellis.mutate('create_run', pg_catalog.jsonb_build_object(
        'session_id', chat_id, 'turn_id', 'turn-terminal',
        'client_request_id', 'request-terminal', 'content', 'Fail',
        'model_id', 'openai:gpt-5.5', 'budget_preset', 'conservative'),
        'TerminalCapabilityWithAtLeastThirtyTwoCharacters123');
    terminal_id := (result->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record', pg_catalog.jsonb_build_object(
        'run_id', terminal_id, 'next_status', 'failed',
        'event_type', 'run.failed', 'data', '{}'::jsonb),
        'TerminalCapabilityWithAtLeastThirtyTwoCharacters123');
END
$check$;

RESET ROLE;
DO $check$
BEGIN
    IF EXISTS (SELECT 1 FROM trellis.runs
        WHERE client_request_id = 'request-terminal' AND lease_token IS NOT NULL) THEN
        RAISE EXCEPTION 'terminal transition retained run capability';
    END IF;
END
$check$;
UPDATE trellis.runs SET lease_expires_at = now() - interval '1 second'
WHERE client_request_id = 'request-expiry';
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000e5', false);

DO $check$
DECLARE
    run_id uuid;
    result jsonb;
BEGIN
    SELECT id INTO run_id FROM trellis.runs WHERE client_request_id = 'request-expiry';
    result := trellis.mutate('recover_expired_run',
        pg_catalog.jsonb_build_object('run_id', run_id));
    IF result->>'status' <> 'interrupted' THEN
        RAISE EXCEPTION 'expired run did not become interrupted';
    END IF;
    IF (SELECT count(*) FROM trellis.run_events WHERE run_events.run_id=(result->>'id')::uuid
        AND event_type='run.interrupted') <> 1 THEN
        RAISE EXCEPTION 'lease recovery event missing';
    END IF;
    BEGIN
        PERFORM trellis.mutate('renew_run_lease',
            pg_catalog.jsonb_build_object('run_id', run_id),
            'ExpiryCapabilityWithAtLeastThirtyTwoCharacters123');
        RAISE EXCEPTION 'recovered run lease renewed';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END
$check$;

RESET ROLE;
