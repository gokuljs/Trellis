\set ON_ERROR_STOP on

INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000f1', 'active@example.test'),
    ('00000000-0000-0000-0000-0000000000f2', 'defaults@example.test'),
    ('00000000-0000-0000-0000-0000000000f3', 'edited@example.test'),
    ('00000000-0000-0000-0000-0000000000f8', 'admission@example.test'),
    ('00000000-0000-0000-0000-0000000000f9', 'missing-rows@example.test');

SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000f1', false);
SELECT trellis.ensure_account();

DO $check$
DECLARE
    v_chat uuid;
    v_run uuid;
    v_next_sequence integer;
    v_result jsonb;
    v_cap text := 'ActiveLegacyImportCapabilityWithThirtyTwoBytes123';
BEGIN
    v_chat := (trellis.mutate('create_session', '{"workspace_path":null}'::jsonb)->>'id')::uuid;
    v_result := trellis.mutate('create_run', pg_catalog.jsonb_build_object(
        'session_id', v_chat, 'turn_id', 'active-turn',
        'client_request_id', 'active-request', 'content', 'Still executing',
        'model_id', 'openai:gpt-5.5', 'budget_preset', 'conservative'),
        'ActiveImportSecurityCapabilityWithThirtyTwoBytes123');
    v_run := (v_result->'run'->>'id')::uuid;
    SELECT last_event_sequence + 1 INTO v_next_sequence FROM trellis.runs WHERE id = v_run;

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
    BEGIN
        PERFORM trellis.import_snapshot(
            '{"table":"profiles","rows":[{"id":"00000000-0000-0000-0000-0000000000f1","display_name":null,"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb,
            v_cap);
        RAISE EXCEPTION 'import began while cloud run was active';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
    END;

    BEGIN
        PERFORM trellis.import_snapshot(pg_catalog.jsonb_build_object(
            'table', 'run_events', 'rows', pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'user_id', '00000000-0000-0000-0000-0000000000f1',
                    'run_id', v_run, 'sequence', v_next_sequence,
                    'event_type', 'run.completed', 'event_version', 1,
                    'data', '{"status":"completed"}'::jsonb,
                    'created_at', '2026-10-01T00:00:00Z',
                    'updated_at', '2026-10-01T00:00:00Z'))), v_cap);
        RAISE EXCEPTION 'import forged an event on an active cloud run';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    IF (SELECT count(*) FROM trellis.run_events WHERE run_id = v_run) <> 1 THEN
        RAISE EXCEPTION 'rejected import changed active run events';
    END IF;
END
$check$;

SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000f2', false);
SELECT trellis.ensure_account();

DO $check$
DECLARE
    v_profile jsonb := '{"table":"profiles","rows":[{"id":"00000000-0000-0000-0000-0000000000f2","display_name":"Historical name","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-02T00:00:00Z"}]}'::jsonb;
    v_settings jsonb := '{"table":"user_settings","rows":[{"user_id":"00000000-0000-0000-0000-0000000000f2","selected_provider":"anthropic","selected_model_id":"anthropic:claude-sonnet-5","default_budget_preset":"longer","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-02T00:00:00Z"}]}'::jsonb;
    v_progress jsonb := '{"table":"onboarding_progress","rows":[{"user_id":"00000000-0000-0000-0000-0000000000f2","flow_version":1,"current_step":"complete","completed_at":"2026-09-03T00:00:00Z","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-03T00:00:00Z"}]}'::jsonb;
    v_cap text := 'DefaultsLegacyImportCapabilityWithThirtyTwoBytes123';
BEGIN
    BEGIN
        PERFORM trellis.import_snapshot(pg_catalog.jsonb_set(
            v_profile, '{rows,0,unexpected}', 'true'::jsonb), v_cap);
        RAISE EXCEPTION 'bootstrap merge accepted an extra column';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    PERFORM trellis.import_snapshot(v_profile, v_cap);
    BEGIN
        PERFORM trellis.mutate('create_session', '{"workspace_path":null}'::jsonb);
        RAISE EXCEPTION 'normal mutation succeeded during legacy import';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
    END;
    BEGIN
        PERFORM trellis.seal_import('WrongLegacyImportCapabilityWithThirtyTwoBytes123');
        RAISE EXCEPTION 'foreign device sealed another device import';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    PERFORM trellis.import_snapshot(v_settings, v_cap);
    PERFORM trellis.import_snapshot(v_progress, v_cap);
    IF (SELECT display_name FROM trellis.profiles WHERE id =
        '00000000-0000-0000-0000-0000000000f2') <> 'Historical name' OR
       (SELECT selected_model_id FROM trellis.user_settings WHERE user_id =
        '00000000-0000-0000-0000-0000000000f2') <> 'anthropic:claude-sonnet-5' OR
       (SELECT current_step FROM trellis.onboarding_progress WHERE user_id =
        '00000000-0000-0000-0000-0000000000f2') <> 'complete' THEN
        RAISE EXCEPTION 'untouched remote defaults did not merge historical settings';
    END IF;

    PERFORM trellis.seal_import(v_cap);
    PERFORM trellis.seal_import(v_cap);
    PERFORM trellis.import_snapshot(v_profile, v_cap);
    PERFORM trellis.import_snapshot(v_settings, v_cap);
    PERFORM trellis.import_snapshot(v_progress, v_cap);
    BEGIN
        PERFORM trellis.import_snapshot('{"table":"chats","rows":[{"id":"00000000-0000-0000-0000-0000000000f4","user_id":"00000000-0000-0000-0000-0000000000f2","title":"Late insert","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb, v_cap);
        RAISE EXCEPTION 'sealed import accepted a new row';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        PERFORM trellis.import_snapshot(v_profile,
            'WrongLegacyImportCapabilityWithThirtyTwoBytes123');
        RAISE EXCEPTION 'sealed replay accepted another device capability';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END
$check$;

SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000f3', false);
SELECT trellis.ensure_account();
SELECT trellis.mutate('update_profile', '{"display_name":"Cloud edit"}'::jsonb);
SELECT trellis.mutate('set_selected_model', '{"model_id":"anthropic:claude-sonnet-5"}'::jsonb);
SELECT trellis.mutate('advance_onboarding_intro', '{}'::jsonb);

DO $check$
DECLARE
    v_cap text := 'EditedLegacyImportCapabilityWithThirtyTwoBytes123';
BEGIN
    PERFORM trellis.import_snapshot('{"table":"profiles","rows":[{"id":"00000000-0000-0000-0000-0000000000f3","display_name":"Older local name","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb, v_cap);
    PERFORM trellis.import_snapshot('{"table":"user_settings","rows":[{"user_id":"00000000-0000-0000-0000-0000000000f3","selected_provider":"openai","selected_model_id":"openai:gpt-5.5","default_budget_preset":"conservative","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb, v_cap);
    PERFORM trellis.import_snapshot('{"table":"onboarding_progress","rows":[{"user_id":"00000000-0000-0000-0000-0000000000f3","flow_version":1,"current_step":"complete","completed_at":"2026-09-01T00:00:00Z","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb, v_cap);
    IF (SELECT display_name FROM trellis.profiles WHERE id =
        '00000000-0000-0000-0000-0000000000f3') <> 'Cloud edit' OR
       (SELECT selected_model_id FROM trellis.user_settings WHERE user_id =
        '00000000-0000-0000-0000-0000000000f3') <> 'anthropic:claude-sonnet-5' OR
       (SELECT current_step FROM trellis.onboarding_progress WHERE user_id =
        '00000000-0000-0000-0000-0000000000f3') <> 'profile' THEN
        RAISE EXCEPTION 'historical import overwrote cloud edits';
    END IF;
END
$check$;

DO $check$
DECLARE
    v_cap text := 'EditedLegacyImportCapabilityWithThirtyTwoBytes123';
    v_event jsonb;
BEGIN
    PERFORM trellis.import_snapshot('{"table":"chats","rows":[{"id":"00000000-0000-0000-0000-0000000000f5","user_id":"00000000-0000-0000-0000-0000000000f3","title":"Historical chat","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb, v_cap);
    PERFORM trellis.import_snapshot('{"table":"messages","rows":[{"id":"00000000-0000-0000-0000-0000000000f6","user_id":"00000000-0000-0000-0000-0000000000f3","chat_id":"00000000-0000-0000-0000-0000000000f5","turn_id":"legacy-turn","ordinal":1,"role":"user","content":"Older input","provider":null,"model":null,"run_id":null,"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb, v_cap);
    PERFORM trellis.import_snapshot('{"table":"runs","rows":[{"id":"00000000-0000-0000-0000-0000000000f7","user_id":"00000000-0000-0000-0000-0000000000f3","chat_id":"00000000-0000-0000-0000-0000000000f5","turn_id":"legacy-turn","input_message_id":"00000000-0000-0000-0000-0000000000f6","client_request_id":"legacy-request","retry_of":null,"status":"failed","provider_id":"openai","model_id":"openai:gpt-5.5","adapter_kind":"openai","upstream_model_id":"gpt-5.5","budget_preset":"conservative","max_model_calls":8,"max_tool_calls":16,"max_total_tokens":100000,"max_cost_usd":2,"deadline_at":"2026-09-01T01:00:00Z","cancel_requested_at":null,"lease_token":null,"lease_expires_at":null,"recovery_count":0,"waiting_tool_call_id":null,"stop_reason":"error","error_code":"test","error_message":"failed","last_event_sequence":2,"started_at":"2026-09-01T00:01:00Z","finished_at":"2026-09-01T00:02:00Z","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:02:00Z"}]}'::jsonb, v_cap);

    v_event := '{"user_id":"00000000-0000-0000-0000-0000000000f3","run_id":"00000000-0000-0000-0000-0000000000f7","sequence":2,"event_type":"run.failed","event_version":1,"data":{},"created_at":"2026-09-01T00:02:00Z","updated_at":"2026-09-01T00:02:00Z"}'::jsonb;
    BEGIN
        PERFORM trellis.import_snapshot(
            pg_catalog.jsonb_build_object('table','run_events','rows',
                pg_catalog.jsonb_build_array(v_event)), v_cap);
        RAISE EXCEPTION 'import accepted an event sequence gap';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    v_event := pg_catalog.jsonb_set(v_event, '{sequence}', '1'::jsonb);
    v_event := pg_catalog.jsonb_set(v_event, '{event_type}', '"run.queued"'::jsonb);
    PERFORM trellis.import_snapshot(pg_catalog.jsonb_build_object(
        'table','run_events','rows',pg_catalog.jsonb_build_array(v_event)), v_cap);
    BEGIN
        PERFORM trellis.seal_import(v_cap);
        RAISE EXCEPTION 'incomplete legacy event history was sealed';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    v_event := pg_catalog.jsonb_set(v_event, '{sequence}', '2'::jsonb);
    v_event := pg_catalog.jsonb_set(v_event, '{event_type}', '"run.completed"'::jsonb);
    BEGIN
        PERFORM trellis.import_snapshot(pg_catalog.jsonb_build_object(
            'table','run_events','rows',pg_catalog.jsonb_build_array(v_event)), v_cap);
        RAISE EXCEPTION 'wrong terminal event imported';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    v_event := pg_catalog.jsonb_set(v_event, '{event_type}', '"run.failed"'::jsonb);
    PERFORM trellis.import_snapshot(pg_catalog.jsonb_build_object(
        'table','run_events','rows',pg_catalog.jsonb_build_array(v_event)), v_cap);
    PERFORM trellis.seal_import(v_cap);
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
    BEGIN
        PERFORM trellis.import_snapshot(
            '{"table":"profiles","rows":[{"id":"00000000-0000-0000-0000-0000000000f8","display_name":"Claimed","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb,
            'AdmissionImportCapabilityWithThirtyTwoBytes123');
        RAISE EXCEPTION 'import began during a classic turn';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
    END;
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
        'session_id', v_chat, 'turn_id', 'classic-turn', 'content', 'Actual input'),
        v_claim_cap);
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

SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000f9', false);

DO $check$
DECLARE
    v_cap text := 'MissingRowsImportCapabilityWithThirtyTwoBytes123';
BEGIN
    BEGIN
        PERFORM trellis.import_snapshot('{"table":"profiles"}'::jsonb, v_cap);
        RAISE EXCEPTION 'missing rows initialized an import';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM trellis.import_snapshot('{"table":"profiles","rows":null}'::jsonb,
            v_cap);
        RAISE EXCEPTION 'null rows initialized an import';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM trellis.import_snapshot(
            '{"rows":[{"id":"00000000-0000-0000-0000-0000000000f9","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}]}'::jsonb,
            v_cap);
        RAISE EXCEPTION 'missing table initialized an import';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
END
$check$;

RESET ROLE;

DO $check$
BEGIN
    IF EXISTS (SELECT 1 FROM trellis_private.import_ledger
        WHERE user_id = '00000000-0000-0000-0000-0000000000f9'
          AND table_name = '__started__') THEN
        RAISE EXCEPTION 'invalid import left a started marker';
    END IF;
END
$check$;
