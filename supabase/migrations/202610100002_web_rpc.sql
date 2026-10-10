-- All writes use audited functions. The exposed wrappers are SECURITY INVOKER;
-- the privileged implementation stays in an unexposed schema and checks the
-- Auth user on every call. A publishable key never grants table write access.
BEGIN;

GRANT USAGE ON SCHEMA trellis_private TO authenticated;

CREATE FUNCTION trellis_private.require_user() RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := auth.uid();
BEGIN
    IF v_user IS NULL THEN
        RAISE EXCEPTION 'authentication required' USING ERRCODE = '42501';
    END IF;
    RETURN v_user;
END
$$;

CREATE FUNCTION trellis_private.capability_digest(p_capability text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF p_capability IS NULL OR pg_catalog.length(p_capability) < 32
       OR pg_catalog.length(p_capability) > 256 THEN
        RAISE EXCEPTION 'invalid run capability' USING ERRCODE = '42501';
    END IF;
    RETURN pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(p_capability, 'UTF8')), 'hex'
    );
END
$$;

CREATE FUNCTION trellis_private.require_run(
    p_run_id uuid, p_capability text
) RETURNS trellis.runs
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_run trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO v_run FROM trellis.runs
    WHERE id = p_run_id AND user_id = v_user FOR UPDATE;
    IF NOT FOUND OR v_run.lease_token IS NULL
       OR v_run.lease_token <> trellis_private.capability_digest(p_capability)
       OR v_run.lease_expires_at IS NULL
       OR v_run.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'run capability invalid or expired' USING ERRCODE = '42501';
    END IF;
    RETURN v_run;
END
$$;

CREATE FUNCTION trellis_private.append_event(
    p_user uuid, p_run_id uuid, p_event_type text, p_data jsonb,
    p_event_version integer DEFAULT 1
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_sequence integer;
    v_event trellis.run_events%ROWTYPE;
BEGIN
    UPDATE trellis.runs SET last_event_sequence = last_event_sequence + 1
    WHERE id = p_run_id AND user_id = p_user
    RETURNING last_event_sequence INTO v_sequence;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run not found' USING ERRCODE = '22023';
    END IF;
    INSERT INTO trellis.run_events (
        user_id, run_id, sequence, event_type, event_version, data
    ) VALUES (p_user, p_run_id, v_sequence, p_event_type,
              p_event_version, p_data)
    RETURNING * INTO v_event;
    RETURN pg_catalog.to_jsonb(v_event);
END
$$;

CREATE FUNCTION trellis_private.ensure_account() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_model trellis.models%ROWTYPE;
BEGIN
    SELECT * INTO v_model FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'model catalog empty' USING ERRCODE = '55000';
    END IF;
    INSERT INTO trellis.profiles(id) VALUES (v_user) ON CONFLICT DO NOTHING;
    INSERT INTO trellis.user_settings(user_id, selected_provider, selected_model_id)
        VALUES (v_user, v_model.provider_id, v_model.id) ON CONFLICT DO NOTHING;
    INSERT INTO trellis.onboarding_progress(user_id)
        VALUES (v_user) ON CONFLICT DO NOTHING;
    RETURN pg_catalog.jsonb_build_object('user_id', v_user);
END
$$;

CREATE FUNCTION trellis.ensure_account() RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT trellis_private.ensure_account()
$$;

CREATE FUNCTION trellis_private.mutate(
    p_action text, p_payload jsonb, p_capability text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_model trellis.models%ROWTYPE;
    v_profile trellis.profiles%ROWTYPE;
    v_progress trellis.onboarding_progress%ROWTYPE;
    v_settings trellis.user_settings%ROWTYPE;
    v_chat trellis.chats%ROWTYPE;
    v_message trellis.messages%ROWTYPE;
    v_run trellis.runs%ROWTYPE;
    v_prior trellis.runs%ROWTYPE;
    v_call trellis.model_calls%ROWTYPE;
    v_tool trellis.tool_calls%ROWTYPE;
    v_run_message trellis.run_messages%ROWTYPE;
    v_event jsonb;
    v_events jsonb := '[]'::jsonb;
    v_json jsonb;
    v_ordinal integer;
    v_chat_id uuid;
    v_run_id uuid;
    v_message_id uuid;
    v_tool_id uuid;
    v_model_call_id uuid;
    v_status text;
    v_event_type text;
    v_content text;
    v_budget text;
    v_deadline_seconds integer;
    v_max_model_calls integer;
    v_max_tool_calls integer;
    v_max_tokens integer;
    v_max_cost numeric;
    v_step integer;
    v_count integer;
    v_item jsonb;
    v_decision text;
    v_approval_at timestamptz;
BEGIN
    IF p_payload IS NULL OR pg_catalog.jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'mutation payload must be an object' USING ERRCODE = '22023';
    END IF;
    PERFORM trellis_private.ensure_account();

    IF p_action = 'update_profile' THEN
        UPDATE trellis.profiles SET display_name = p_payload->>'display_name'
        WHERE id = v_user RETURNING * INTO v_profile;
        RETURN pg_catalog.to_jsonb(v_profile);

    ELSIF p_action = 'advance_onboarding_intro' THEN
        UPDATE trellis.onboarding_progress SET current_step = 'profile'
        WHERE user_id = v_user AND current_step = 'intro';
        SELECT * INTO v_progress FROM trellis.onboarding_progress WHERE user_id = v_user;
        RETURN pg_catalog.to_jsonb(v_progress);

    ELSIF p_action = 'save_onboarding_profile' THEN
        IF pg_catalog.btrim(p_payload->>'display_name') = '' THEN
            RAISE EXCEPTION 'display name required' USING ERRCODE = '22023';
        END IF;
        UPDATE trellis.profiles SET display_name = p_payload->>'display_name'
        WHERE id = v_user RETURNING * INTO v_profile;
        UPDATE trellis.onboarding_progress
        SET current_step = CASE WHEN current_step = 'profile' THEN 'model'
                                ELSE current_step END
        WHERE user_id = v_user RETURNING * INTO v_progress;
        RETURN pg_catalog.jsonb_build_object(
            'profile', pg_catalog.to_jsonb(v_profile),
            'progress', pg_catalog.to_jsonb(v_progress)
        );

    ELSIF p_action = 'complete_onboarding' THEN
        SELECT * INTO v_model FROM trellis.models
        WHERE id = p_payload->>'model_id' AND enabled;
        SELECT * INTO v_progress FROM trellis.onboarding_progress WHERE user_id = v_user;
        IF v_model.id IS NULL OR v_progress.current_step NOT IN ('model', 'complete') THEN
            RETURN 'false'::jsonb;
        END IF;
        UPDATE trellis.user_settings SET selected_provider = v_model.provider_id,
            selected_model_id = v_model.id WHERE user_id = v_user;
        UPDATE trellis.onboarding_progress SET current_step = 'complete',
            completed_at = COALESCE(completed_at, v_now) WHERE user_id = v_user;
        RETURN 'true'::jsonb;

    ELSIF p_action = 'set_selected_provider' THEN
        SELECT * INTO v_model FROM trellis.models
        WHERE provider_id = p_payload->>'provider' AND enabled
        ORDER BY catalog_order LIMIT 1;
        IF v_model.id IS NOT NULL THEN
            UPDATE trellis.user_settings SET selected_provider = v_model.provider_id,
                selected_model_id = v_model.id WHERE user_id = v_user;
        END IF;
        RETURN pg_catalog.to_jsonb(p_payload->>'provider');

    ELSIF p_action = 'set_selected_model' THEN
        SELECT * INTO v_model FROM trellis.models
        WHERE id = p_payload->>'model_id' AND enabled;
        IF v_model.id IS NULL THEN RETURN 'false'::jsonb; END IF;
        UPDATE trellis.user_settings SET selected_provider = v_model.provider_id,
            selected_model_id = v_model.id WHERE user_id = v_user;
        RETURN 'true'::jsonb;

    ELSIF p_action = 'set_default_budget_preset' THEN
        IF p_payload->>'preset' NOT IN ('conservative', 'longer') THEN
            RAISE EXCEPTION 'unknown run budget' USING ERRCODE = '22023';
        END IF;
        UPDATE trellis.user_settings SET default_budget_preset = p_payload->>'preset'
        WHERE user_id = v_user;
        RETURN 'null'::jsonb;

    ELSIF p_action = 'create_session' THEN
        INSERT INTO trellis.chats(user_id, workspace_path)
        VALUES (v_user, p_payload->>'workspace_path') RETURNING * INTO v_chat;
        RETURN pg_catalog.to_jsonb(v_chat);

    ELSIF p_action = 'set_session_workspace' THEN
        v_chat_id := (p_payload->>'session_id')::uuid;
        SELECT * INTO v_chat FROM trellis.chats
        WHERE id = v_chat_id AND user_id = v_user FOR UPDATE;
        IF NOT FOUND THEN RETURN 'null'::jsonb; END IF;
        IF EXISTS (SELECT 1 FROM trellis.runs WHERE user_id = v_user
                   AND chat_id = v_chat_id AND status IN
                   ('queued', 'running', 'waiting_for_approval', 'cancelling')) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'session_workspace_busy';
        END IF;
        UPDATE trellis.chats SET workspace_path = p_payload->>'workspace_path'
        WHERE id = v_chat_id AND user_id = v_user RETURNING * INTO v_chat;
        RETURN pg_catalog.to_jsonb(v_chat);

    ELSIF p_action = 'claim_turn' THEN
        v_chat_id := (p_payload->>'session_id')::uuid;
        PERFORM 1 FROM trellis.chats WHERE id = v_chat_id AND user_id = v_user;
        IF NOT FOUND THEN RETURN 'false'::jsonb; END IF;
        DELETE FROM trellis.turn_claims WHERE user_id = v_user
            AND claimed_at <= v_now - interval '5 minutes';
        INSERT INTO trellis.turn_claims(user_id, chat_id, turn_id)
        VALUES (v_user, v_chat_id, p_payload->>'turn_id')
        ON CONFLICT DO NOTHING;
        RETURN pg_catalog.to_jsonb(FOUND);

    ELSIF p_action = 'release_turn' THEN
        DELETE FROM trellis.turn_claims WHERE user_id = v_user
          AND chat_id = (p_payload->>'session_id')::uuid
          AND turn_id = p_payload->>'turn_id';
        RETURN 'null'::jsonb;

    ELSIF p_action IN ('add_user_message', 'add_assistant_message') THEN
        v_chat_id := (p_payload->>'session_id')::uuid;
        SELECT * INTO v_chat FROM trellis.chats
        WHERE id = v_chat_id AND user_id = v_user FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'session not found' USING ERRCODE = '22023'; END IF;
        SELECT COALESCE(MAX(ordinal), 0) + 1 INTO v_ordinal FROM trellis.messages
        WHERE user_id = v_user AND chat_id = v_chat_id;
        INSERT INTO trellis.messages (
            user_id, chat_id, turn_id, ordinal, role, content, provider, model
        ) VALUES (
            v_user, v_chat_id, p_payload->>'turn_id', v_ordinal,
            CASE WHEN p_action = 'add_user_message' THEN 'user' ELSE 'assistant' END,
            p_payload->>'content',
            CASE WHEN p_action = 'add_assistant_message' THEN p_payload->>'provider' END,
            CASE WHEN p_action = 'add_assistant_message' THEN p_payload->>'model' END
        ) RETURNING * INTO v_message;
        UPDATE trellis.chats SET
            title = CASE WHEN v_ordinal = 1 AND p_action = 'add_user_message'
                THEN COALESCE(NULLIF(pg_catalog.left(pg_catalog.regexp_replace(
                    pg_catalog.btrim(v_message.content), '\s+', ' ', 'g'), 80), ''), 'New session')
                ELSE title END,
            updated_at = v_now
        WHERE id = v_chat_id AND user_id = v_user;
        RETURN pg_catalog.to_jsonb(v_message);

    ELSIF p_action = 'create_run' THEN
        v_chat_id := (p_payload->>'session_id')::uuid;
        v_content := pg_catalog.btrim(p_payload->>'content');
        v_budget := COALESCE(p_payload->>'budget_preset', 'conservative');
        IF v_content IS NULL OR v_content = '' THEN
            RAISE EXCEPTION 'run input cannot be empty' USING ERRCODE = '22023';
        END IF;
        IF pg_catalog.length(p_payload->>'client_request_id') NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'invalid client request ID' USING ERRCODE = '22023';
        END IF;
        IF v_budget = 'conservative' THEN
            v_max_model_calls := 8; v_max_tool_calls := 16;
            v_max_tokens := 100000; v_max_cost := 2; v_deadline_seconds := 600;
        ELSIF v_budget = 'longer' THEN
            v_max_model_calls := 15; v_max_tool_calls := 30;
            v_max_tokens := 200000; v_max_cost := 5; v_deadline_seconds := 1200;
        ELSE
            RAISE EXCEPTION 'unknown run budget' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_model FROM trellis.models
        WHERE id = p_payload->>'model_id' AND enabled;
        IF v_model.id IS NULL THEN
            RAISE EXCEPTION 'model not found' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_chat FROM trellis.chats
        WHERE id = v_chat_id AND user_id = v_user FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'session not found' USING ERRCODE = '22023'; END IF;

        SELECT * INTO v_run FROM trellis.runs WHERE user_id = v_user
            AND chat_id = v_chat_id AND client_request_id = p_payload->>'client_request_id';
        IF FOUND THEN
            SELECT * INTO v_message FROM trellis.messages
            WHERE id = v_run.input_message_id AND user_id = v_user;
            IF v_run.turn_id <> p_payload->>'turn_id'
               OR v_message.content <> v_content
               OR v_run.model_id <> v_model.id OR v_run.budget_preset <> v_budget THEN
                RAISE EXCEPTION 'run request conflicts with a previous payload'
                    USING ERRCODE = '22023';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'run', pg_catalog.to_jsonb(v_run) - 'lease_token',
                'lease_acquired', false
            );
        END IF;

        SELECT * INTO v_prior FROM trellis.runs WHERE user_id = v_user
            AND chat_id = v_chat_id AND turn_id = p_payload->>'turn_id'
            ORDER BY created_at DESC, id DESC LIMIT 1;
        IF FOUND THEN
            SELECT * INTO v_message FROM trellis.messages
            WHERE id = v_prior.input_message_id AND user_id = v_user;
            IF v_message.content <> v_content THEN
                RAISE EXCEPTION 'run request conflicts with a previous payload'
                    USING ERRCODE = '22023';
            END IF;
            IF v_prior.status IN ('queued', 'running', 'waiting_for_approval',
                                  'cancelling', 'completed') THEN
                RETURN pg_catalog.jsonb_build_object(
                    'run', pg_catalog.to_jsonb(v_prior) - 'lease_token',
                    'lease_acquired', false
                );
            END IF;
        END IF;

        IF EXISTS (SELECT 1 FROM trellis.runs WHERE user_id = v_user
                   AND chat_id = v_chat_id AND status IN
                   ('queued', 'running', 'waiting_for_approval', 'cancelling')) THEN
            RAISE EXCEPTION 'active run already exists for this session'
                USING ERRCODE = '22023';
        END IF;

        SELECT * INTO v_message FROM trellis.messages
        WHERE user_id = v_user AND chat_id = v_chat_id
          AND turn_id = p_payload->>'turn_id' AND role = 'user';
        IF FOUND THEN
            IF v_message.content <> v_content THEN
                RAISE EXCEPTION 'run request conflicts with a previous payload'
                    USING ERRCODE = '22023';
            END IF;
        ELSE
            SELECT COALESCE(MAX(ordinal), 0) + 1 INTO v_ordinal
            FROM trellis.messages WHERE user_id = v_user AND chat_id = v_chat_id;
            INSERT INTO trellis.messages(user_id, chat_id, turn_id, ordinal,
                                         role, content)
            VALUES (v_user, v_chat_id, p_payload->>'turn_id', v_ordinal,
                    'user', v_content) RETURNING * INTO v_message;
            IF v_ordinal = 1 THEN
                UPDATE trellis.chats SET title = COALESCE(NULLIF(
                    pg_catalog.left(pg_catalog.regexp_replace(v_content, '\s+', ' ', 'g'), 80),
                    ''), 'New session')
                WHERE id = v_chat_id AND user_id = v_user;
            END IF;
        END IF;

        INSERT INTO trellis.runs (
            user_id, chat_id, turn_id, input_message_id, client_request_id,
            retry_of, status, provider_id, model_id, adapter_kind,
            upstream_model_id, budget_preset, max_model_calls, max_tool_calls,
            max_total_tokens, max_cost_usd, deadline_at, lease_token,
            lease_expires_at
        ) VALUES (
            v_user, v_chat_id, p_payload->>'turn_id', v_message.id,
            p_payload->>'client_request_id', v_prior.id, 'queued',
            v_model.provider_id, v_model.id, v_model.adapter_kind,
            v_model.upstream_model_id, v_budget, v_max_model_calls, v_max_tool_calls,
            v_max_tokens, v_max_cost, v_now + pg_catalog.make_interval(secs => v_deadline_seconds),
            trellis_private.capability_digest(p_capability), v_now + interval '60 seconds'
        ) RETURNING * INTO v_run;
        UPDATE trellis.messages SET run_id = v_run.id WHERE id = v_message.id
            AND user_id = v_user;
        v_event := trellis_private.append_event(v_user, v_run.id, 'run.queued',
            pg_catalog.jsonb_build_object('session_id', v_chat_id,
                                         'turn_id', v_run.turn_id, 'status', 'queued'));
        SELECT * INTO v_run FROM trellis.runs WHERE id = v_run.id;
        RETURN pg_catalog.jsonb_build_object(
            'run', pg_catalog.to_jsonb(v_run) - 'lease_token',
            'lease_acquired', true
        );

    ELSIF p_action = 'append_run_event' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        v_event_type := p_payload->>'event_type';
        IF v_run.status IN ('completed', 'failed', 'cancelled', 'interrupted')
           OR v_event_type IN (
              'run.queued', 'run.started', 'run.cancellation_requested',
              'tool.approval_requested', 'run.resumed', 'run.completed',
              'run.failed', 'run.cancelled', 'run.interrupted'
           ) THEN
            RAISE EXCEPTION 'invalid event for run state' USING ERRCODE = '22023';
        END IF;
        RETURN trellis_private.append_event(v_user, v_run_id, v_event_type,
            COALESCE(p_payload->'data', '{}'::jsonb),
            COALESCE((p_payload->>'event_version')::integer, 1));

    ELSIF p_action = 'transition_run_record' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        v_status := p_payload->>'next_status';
        v_event_type := p_payload->>'event_type';
        IF NOT (
            (v_run.status = 'queued' AND v_status = 'running' AND v_event_type = 'run.started') OR
            (v_run.status = 'queued' AND v_status = 'failed' AND v_event_type = 'run.failed') OR
            (v_run.status = 'queued' AND v_status = 'cancelled' AND v_event_type = 'run.cancelled') OR
            (v_run.status = 'queued' AND v_status = 'interrupted' AND v_event_type = 'run.interrupted') OR
            (v_run.status = 'running' AND v_status = 'cancelling' AND v_event_type = 'run.cancellation_requested') OR
            (v_run.status = 'running' AND v_status = 'completed' AND v_event_type = 'run.completed') OR
            (v_run.status = 'running' AND v_status = 'failed' AND v_event_type = 'run.failed') OR
            (v_run.status = 'running' AND v_status = 'cancelled' AND v_event_type = 'run.cancelled') OR
            (v_run.status = 'running' AND v_status = 'interrupted' AND v_event_type = 'run.interrupted') OR
            (v_run.status = 'cancelling' AND v_status = 'cancelled' AND v_event_type = 'run.cancelled') OR
            (v_run.status = 'cancelling' AND v_status = 'failed' AND v_event_type = 'run.failed') OR
            (v_run.status = 'cancelling' AND v_status = 'interrupted' AND v_event_type = 'run.interrupted')
        ) THEN
            RAISE EXCEPTION 'invalid run transition or lifecycle event' USING ERRCODE = '22023';
        END IF;
        UPDATE trellis.runs SET status = v_status,
            started_at = CASE WHEN v_status = 'running' THEN COALESCE(started_at, v_now)
                              ELSE started_at END,
            finished_at = CASE WHEN v_status IN ('completed', 'failed', 'cancelled', 'interrupted')
                               THEN v_now ELSE NULL END,
            error_code = p_payload->>'error_code',
            error_message = p_payload->>'error_message',
            stop_reason = p_payload->>'stop_reason',
            waiting_tool_call_id = CASE WHEN v_status IN
                ('completed', 'failed', 'cancelled', 'interrupted') THEN NULL
                ELSE waiting_tool_call_id END,
            lease_token = CASE WHEN v_status IN
                ('completed', 'failed', 'cancelled', 'interrupted') THEN NULL
                ELSE lease_token END,
            lease_expires_at = CASE WHEN v_status IN
                ('completed', 'failed', 'cancelled', 'interrupted') THEN NULL
                ELSE lease_expires_at END
        WHERE id = v_run_id AND user_id = v_user;
        RETURN trellis_private.append_event(v_user, v_run_id, v_event_type,
            COALESCE(p_payload->'data', '{}'::jsonb));

    ELSIF p_action = 'complete_run' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        v_content := pg_catalog.btrim(p_payload->>'content');
        IF v_run.status <> 'running' OR v_content IS NULL OR v_content = '' THEN
            RAISE EXCEPTION 'run not running or assistant response empty' USING ERRCODE = '22023';
        END IF;
        PERFORM 1 FROM trellis.chats WHERE id = v_run.chat_id AND user_id = v_user FOR UPDATE;
        SELECT COALESCE(MAX(ordinal), 0) + 1 INTO v_ordinal FROM trellis.messages
            WHERE user_id = v_user AND chat_id = v_run.chat_id;
        INSERT INTO trellis.messages (user_id, chat_id, turn_id, ordinal, role,
                                      content, provider, model, run_id)
        VALUES (v_user, v_run.chat_id, v_run.turn_id, v_ordinal, 'assistant',
                v_content, v_run.provider_id, v_run.model_id, v_run_id)
        RETURNING * INTO v_message;
        UPDATE trellis.chats SET updated_at = v_now WHERE id = v_run.chat_id;
        v_event := trellis_private.append_event(v_user, v_run_id,
            'assistant.completed', pg_catalog.jsonb_build_object('message_id', v_message.id));
        v_events := pg_catalog.jsonb_build_array(v_event);
        UPDATE trellis.runs SET status = 'completed', finished_at = v_now,
            stop_reason = 'final_response', lease_token = NULL,
            lease_expires_at = NULL WHERE id = v_run_id AND user_id = v_user;
        v_event := trellis_private.append_event(v_user, v_run_id, 'run.completed',
            pg_catalog.jsonb_build_object('message_id', v_message.id,
                                          'status', 'completed',
                                          'stop_reason', 'final_response'));
        RETURN pg_catalog.jsonb_build_object(
            'message', pg_catalog.to_jsonb(v_message),
            'events', v_events || pg_catalog.jsonb_build_array(v_event));

    ELSIF p_action = 'request_run_cancellation' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        IF v_run.status IN ('cancelling', 'completed', 'failed', 'cancelled', 'interrupted') THEN
            RETURN pg_catalog.jsonb_build_object(
                'run', pg_catalog.to_jsonb(v_run) - 'lease_token', 'event', null);
        END IF;
        IF v_run.status = 'queued' THEN
            v_status := 'cancelled'; v_event_type := 'run.cancelled';
            v_json := pg_catalog.jsonb_build_object('status', 'cancelled',
                'code', 'user_cancelled', 'message', 'The run was cancelled.');
        ELSE
            v_status := 'cancelling'; v_event_type := 'run.cancellation_requested';
            v_json := pg_catalog.jsonb_build_object('status', 'cancelling');
        END IF;
        UPDATE trellis.runs SET status = v_status,
            waiting_tool_call_id = NULL,
            cancel_requested_at = COALESCE(cancel_requested_at, v_now),
            finished_at = CASE WHEN v_status = 'cancelled' THEN v_now ELSE NULL END,
            stop_reason = CASE WHEN v_status = 'cancelled' THEN 'user_cancelled'
                               ELSE stop_reason END,
            lease_token = CASE WHEN v_status = 'cancelled' THEN NULL
                               ELSE lease_token END,
            lease_expires_at = CASE WHEN v_status = 'cancelled' THEN NULL
                                    ELSE lease_expires_at END
        WHERE id = v_run_id AND user_id = v_user;
        v_event := trellis_private.append_event(v_user, v_run_id,
            v_event_type, v_json);
        SELECT * INTO v_run FROM trellis.runs WHERE id = v_run_id;
        RETURN pg_catalog.jsonb_build_object(
            'run', pg_catalog.to_jsonb(v_run) - 'lease_token', 'event', v_event);

    ELSIF p_action = 'create_model_call' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        v_step := (p_payload->>'step_index')::integer;
        IF v_run.status <> 'running' OR v_run.waiting_tool_call_id IS NOT NULL
           OR v_step IS NULL OR v_step < 1 THEN
            RAISE EXCEPTION 'run not ready for model call' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_model FROM trellis.models
        WHERE id = p_payload->>'model_id' AND enabled;
        IF NOT FOUND OR v_model.id <> v_run.model_id THEN
            RAISE EXCEPTION 'model differs from run' USING ERRCODE = '22023';
        END IF;
        INSERT INTO trellis.model_calls (user_id, run_id, step_index,
            provider_id, model_id, adapter_kind, status, request_snapshot, started_at)
        VALUES (v_user, v_run_id, v_step, v_model.provider_id, v_model.id,
            v_model.adapter_kind, 'pending', p_payload->'request_snapshot', v_now)
        RETURNING * INTO v_call;
        RETURN pg_catalog.to_jsonb(v_call);

    ELSIF p_action = 'update_model_call' THEN
        v_model_call_id := (p_payload->>'call_id')::uuid;
        SELECT * INTO v_call FROM trellis.model_calls
        WHERE id = v_model_call_id AND user_id = v_user FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'model call not found' USING ERRCODE = '22023'; END IF;
        v_run := trellis_private.require_run(v_call.run_id, p_capability);
        v_status := p_payload->>'next_status';
        IF v_call.status NOT IN ('pending', 'streaming') OR
           v_status NOT IN ('streaming', 'completed', 'failed', 'cancelled', 'timed_out') THEN
            RAISE EXCEPTION 'invalid model call transition' USING ERRCODE = '22023';
        END IF;
        UPDATE trellis.model_calls SET status = v_status,
            response_snapshot = COALESCE(p_payload->'response_snapshot', response_snapshot),
            provider_response_id = COALESCE(p_payload->>'provider_response_id', provider_response_id),
            finish_reason = COALESCE(p_payload->>'finish_reason', finish_reason),
            input_tokens = COALESCE((p_payload->>'input_tokens')::integer, input_tokens),
            output_tokens = COALESCE((p_payload->>'output_tokens')::integer, output_tokens),
            reasoning_tokens = COALESCE((p_payload->>'reasoning_tokens')::integer, reasoning_tokens),
            cached_read_tokens = COALESCE((p_payload->>'cached_read_tokens')::integer, cached_read_tokens),
            cache_creation_tokens = COALESCE((p_payload->>'cache_creation_tokens')::integer, cache_creation_tokens),
            estimated_cost = COALESCE((p_payload->>'estimated_cost')::numeric, estimated_cost),
            error_code = COALESCE(p_payload->>'error_code', error_code),
            error_message = COALESCE(p_payload->>'error_message', error_message),
            finished_at = CASE WHEN v_status = 'streaming' THEN NULL ELSE v_now END
        WHERE id = v_model_call_id AND user_id = v_user RETURNING * INTO v_call;
        RETURN pg_catalog.to_jsonb(v_call);

    ELSIF p_action = 'record_assistant_message' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_model_call_id := (p_payload->>'model_call_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        v_json := p_payload->'message';
        IF v_run.status <> 'running' OR v_run.waiting_tool_call_id IS NOT NULL
           OR v_json->>'role' <> 'assistant' OR v_json->>'tool_call_id' IS NOT NULL
           OR (pg_catalog.btrim(COALESCE(v_json->>'content', '')) = ''
               AND pg_catalog.jsonb_array_length(COALESCE(v_json->'tool_calls', '[]'::jsonb)) = 0)
           OR pg_catalog.octet_length(COALESCE(v_json->'continuation_items', '[]'::jsonb)::text) > 4194304 THEN
            RAISE EXCEPTION 'invalid assistant run message' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_call FROM trellis.model_calls
        WHERE id = v_model_call_id AND user_id = v_user AND run_id = v_run_id
          AND status = 'completed';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'completed model call not found for run' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(MAX(ordinal), 0) + 1 INTO v_ordinal FROM trellis.run_messages
        WHERE user_id = v_user AND run_id = v_run_id;
        INSERT INTO trellis.run_messages (
            user_id, run_id, ordinal, role, content, continuation, model_call_id
        ) VALUES (
            v_user, v_run_id, v_ordinal, 'assistant', COALESCE(v_json->>'content', ''),
            COALESCE(v_json->'continuation_items', '[]'::jsonb), v_model_call_id
        ) RETURNING * INTO v_run_message;
        v_event := trellis_private.append_event(v_user, v_run_id,
            'assistant.message', pg_catalog.jsonb_build_object(
                'message_id', v_run_message.id, 'model_call_id', v_model_call_id,
                'content', v_run_message.content));
        v_events := pg_catalog.jsonb_build_array(v_event);
        v_json := '[]'::jsonb;
        v_count := 0;
        FOR v_item IN SELECT value FROM pg_catalog.jsonb_array_elements(
            COALESCE(p_payload->'message'->'tool_calls', '[]'::jsonb)
        ) LOOP
            IF v_item->>'id' IS NULL OR v_item->>'name' IS NULL
               OR pg_catalog.jsonb_typeof(v_item->'arguments') <> 'object' THEN
                RAISE EXCEPTION 'invalid tool call' USING ERRCODE = '22023';
            END IF;
            INSERT INTO trellis.tool_calls (
                user_id, run_id, assistant_message_id, call_index,
                provider_call_id, name, arguments, status
            ) VALUES (
                v_user, v_run_id, v_run_message.id, v_count,
                v_item->>'id', v_item->>'name', v_item->'arguments', 'pending'
            ) RETURNING * INTO v_tool;
            v_json := v_json || pg_catalog.jsonb_build_array(pg_catalog.to_jsonb(v_tool));
            v_event := trellis_private.append_event(v_user, v_run_id,
                'tool.call', pg_catalog.jsonb_build_object(
                    'tool_call_id', v_tool.id,
                    'provider_call_id', v_tool.provider_call_id,
                    'name', v_tool.name,
                    'arguments', v_tool.arguments,
                    'assistant_message_id', v_run_message.id));
            v_events := v_events || pg_catalog.jsonb_build_array(v_event);
            v_count := v_count + 1;
        END LOOP;
        RETURN pg_catalog.jsonb_build_object(
            'message', pg_catalog.to_jsonb(v_run_message),
            'tool_calls', v_json, 'events', v_events);

    ELSIF p_action = 'record_tool_result' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_tool_id := (p_payload->>'tool_call_id')::uuid;
        v_status := COALESCE(p_payload->>'status', 'completed');
        v_run := trellis_private.require_run(v_run_id, p_capability);
        IF v_status NOT IN ('completed', 'failed', 'denied', 'cancelled', 'timed_out')
           OR v_run.waiting_tool_call_id IS NOT NULL
           OR NOT (v_run.status = 'running'
                   OR (v_run.status = 'cancelling' AND v_status = 'cancelled')) THEN
            RAISE EXCEPTION 'run not ready for tool result' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_tool FROM trellis.tool_calls WHERE id = v_tool_id
            AND user_id = v_user AND run_id = v_run_id FOR UPDATE;
        IF NOT FOUND OR v_tool.status NOT IN ('pending', 'running')
           OR (v_tool.approval_decision = 'denied' AND v_status <> 'denied') THEN
            RAISE EXCEPTION 'tool call cannot accept result' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(MAX(ordinal), 0) + 1 INTO v_ordinal FROM trellis.run_messages
        WHERE user_id = v_user AND run_id = v_run_id;
        INSERT INTO trellis.run_messages (
            user_id, run_id, ordinal, role, content, tool_call_id
        ) VALUES (
            v_user, v_run_id, v_ordinal, 'tool', p_payload->>'content', v_tool_id
        ) RETURNING * INTO v_run_message;
        UPDATE trellis.tool_calls SET status = v_status, finished_at = v_now
        WHERE id = v_tool_id AND user_id = v_user RETURNING * INTO v_tool;
        v_event := trellis_private.append_event(v_user, v_run_id,
            'tool.result', pg_catalog.jsonb_build_object(
                'tool_call_id', v_tool_id, 'message_id', v_run_message.id,
                'status', v_status, 'content', v_run_message.content));
        RETURN pg_catalog.jsonb_build_object(
            'message', pg_catalog.to_jsonb(v_run_message),
            'tool_call', pg_catalog.to_jsonb(v_tool), 'event', v_event);

    ELSIF p_action = 'request_tool_approval' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_tool_id := (p_payload->>'tool_call_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        IF v_run.status <> 'running' OR v_run.waiting_tool_call_id IS NOT NULL
           OR pg_catalog.octet_length(COALESCE(p_payload->'preview', 'null'::jsonb)::text) > 128000 THEN
            RAISE EXCEPTION 'run not ready for tool approval' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_tool FROM trellis.tool_calls WHERE id = v_tool_id
            AND user_id = v_user AND run_id = v_run_id FOR UPDATE;
        IF NOT FOUND OR v_tool.status <> 'pending' OR v_tool.approval_decision IS NOT NULL THEN
            RAISE EXCEPTION 'pending tool call not found for approval'
                USING ERRCODE = '22023';
        END IF;
        UPDATE trellis.tool_calls SET approval_preview = p_payload->'preview'
        WHERE id = v_tool_id AND user_id = v_user;
        UPDATE trellis.runs SET status = 'waiting_for_approval',
            waiting_tool_call_id = v_tool_id WHERE id = v_run_id AND user_id = v_user;
        v_event := trellis_private.append_event(v_user, v_run_id,
            'tool.approval_requested', pg_catalog.jsonb_build_object(
                'tool_call_id', v_tool_id, 'name', v_tool.name,
                'arguments', v_tool.arguments, 'preview', p_payload->'preview'));
        SELECT * INTO v_run FROM trellis.runs WHERE id = v_run_id;
        RETURN pg_catalog.jsonb_build_object(
            'run', pg_catalog.to_jsonb(v_run) - 'lease_token', 'event', v_event);

    ELSIF p_action = 'record_tool_approval_decision' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_tool_id := (p_payload->>'tool_call_id')::uuid;
        v_decision := p_payload->>'decision';
        v_run := trellis_private.require_run(v_run_id, p_capability);
        IF v_run.status <> 'waiting_for_approval'
           OR v_run.waiting_tool_call_id IS DISTINCT FROM v_tool_id
           OR v_decision NOT IN ('approved', 'denied') THEN
            RAISE EXCEPTION 'run not waiting for this tool approval'
                USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_tool FROM trellis.tool_calls WHERE id = v_tool_id
            AND user_id = v_user AND run_id = v_run_id FOR UPDATE;
        IF NOT FOUND OR v_tool.status <> 'pending' THEN
            RAISE EXCEPTION 'pending tool call not found' USING ERRCODE = '22023';
        END IF;
        IF v_tool.approval_decision IS NOT NULL THEN
            IF v_tool.approval_decision <> v_decision THEN
                RAISE EXCEPTION 'tool approval decision already differs'
                    USING ERRCODE = '22023';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'tool_call', pg_catalog.to_jsonb(v_tool), 'event', null);
        END IF;
        UPDATE trellis.tool_calls SET approval_decision = v_decision,
            approval_decided_at = v_now WHERE id = v_tool_id AND user_id = v_user
            RETURNING * INTO v_tool;
        v_event := trellis_private.append_event(v_user, v_run_id,
            'tool.approval_decided', pg_catalog.jsonb_build_object(
                'tool_call_id', v_tool_id, 'decision', v_decision));
        RETURN pg_catalog.jsonb_build_object(
            'tool_call', pg_catalog.to_jsonb(v_tool), 'event', v_event);

    ELSIF p_action = 'resume_approved_run' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        IF v_run.status <> 'waiting_for_approval'
           OR v_run.waiting_tool_call_id IS NULL THEN
            RAISE EXCEPTION 'run not waiting for approval' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_tool FROM trellis.tool_calls
        WHERE id = v_run.waiting_tool_call_id AND user_id = v_user
          AND run_id = v_run_id AND status = 'pending'
          AND approval_decision IS NOT NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'waiting tool call has no approval decision'
                USING ERRCODE = '22023';
        END IF;
        SELECT created_at INTO v_approval_at FROM trellis.run_events
        WHERE user_id = v_user AND run_id = v_run_id
          AND event_type = 'tool.approval_requested'
          AND data->>'tool_call_id' = v_tool.id::text
        ORDER BY sequence DESC LIMIT 1;
        IF v_approval_at IS NULL THEN
            RAISE EXCEPTION 'waiting tool call has no approval request'
                USING ERRCODE = '22023';
        END IF;
        UPDATE trellis.runs SET status = 'running', waiting_tool_call_id = NULL,
            deadline_at = deadline_at + GREATEST(v_now - v_approval_at, interval '0 seconds')
        WHERE id = v_run_id AND user_id = v_user RETURNING * INTO v_run;
        v_event := trellis_private.append_event(v_user, v_run_id,
            'run.resumed', pg_catalog.jsonb_build_object(
                'tool_call_id', v_tool.id, 'deadline_at', v_run.deadline_at));
        RETURN pg_catalog.jsonb_build_object(
            'run', pg_catalog.to_jsonb(v_run) - 'lease_token', 'event', v_event);

    ELSIF p_action = 'renew_run_lease' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        v_run := trellis_private.require_run(v_run_id, p_capability);
        IF v_run.status NOT IN ('queued', 'running', 'waiting_for_approval', 'cancelling') THEN
            RAISE EXCEPTION 'run is not active' USING ERRCODE = '22023';
        END IF;
        UPDATE trellis.runs SET lease_expires_at = v_now + interval '60 seconds'
        WHERE id = v_run_id AND user_id = v_user RETURNING * INTO v_run;
        RETURN pg_catalog.jsonb_build_object(
            'run', pg_catalog.to_jsonb(v_run) - 'lease_token', 'lease_acquired', true
        );

    ELSIF p_action = 'recover_expired_run' THEN
        v_run_id := (p_payload->>'run_id')::uuid;
        SELECT * INTO v_run FROM trellis.runs WHERE id = v_run_id
            AND user_id = v_user FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE = '42501'; END IF;
        IF v_run.status NOT IN ('queued', 'running', 'waiting_for_approval', 'cancelling') THEN
            RETURN pg_catalog.to_jsonb(v_run) - 'lease_token';
        END IF;
        IF v_run.lease_expires_at IS NULL THEN
            RAISE EXCEPTION 'active run has no lease' USING ERRCODE = '42501';
        END IF;
        IF v_run.lease_expires_at > v_now THEN
            RETURN pg_catalog.to_jsonb(v_run) - 'lease_token';
        END IF;
        UPDATE trellis.runs SET status = 'interrupted', finished_at = v_now,
            stop_reason = 'lease_expired', waiting_tool_call_id = NULL,
            lease_token = NULL, lease_expires_at = NULL,
            recovery_count = recovery_count + 1
        WHERE id = v_run_id AND user_id = v_user;
        PERFORM trellis_private.append_event(v_user, v_run_id, 'run.interrupted',
            pg_catalog.jsonb_build_object('status', 'interrupted',
                'code', 'lease_expired', 'message', 'The run lease expired.'));
        SELECT * INTO v_run FROM trellis.runs WHERE id = v_run_id;
        RETURN pg_catalog.to_jsonb(v_run) - 'lease_token';

    ELSE
        RAISE EXCEPTION 'unknown mutation action: %', p_action
            USING ERRCODE = '22023';
    END IF;
END
$$;

CREATE FUNCTION trellis.mutate(
    p_action text, p_payload jsonb, p_capability text DEFAULT NULL
) RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT trellis_private.mutate(p_action, p_payload, p_capability)
$$;

REVOKE ALL ON FUNCTION trellis_private.require_user() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.capability_digest(text) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.require_run(uuid,text) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.append_event(uuid,uuid,text,jsonb,integer) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.ensure_account() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis.ensure_account() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis.mutate(text,jsonb,text) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION trellis_private.ensure_account() TO authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
GRANT EXECUTE ON FUNCTION trellis.ensure_account() TO authenticated;
GRANT EXECUTE ON FUNCTION trellis.mutate(text,jsonb,text) TO authenticated;

COMMIT;
