-- Close the one-time legacy import after its source has been copied. An
-- authenticated second device must not be able to append events or transcript
-- rows to a live run, even when it owns the same Supabase account.
BEGIN;

-- Classic chat turns are local operations too. A second computer with the
-- same Auth token may observe the chat, but cannot release its claim or fill
-- its assistant slot without the local claim capability.
ALTER TABLE trellis.turn_claims ADD COLUMN claim_token_hash text
    CHECK (claim_token_hash IS NULL OR
           claim_token_hash ~ '^[0-9a-f]{64}$');
REVOKE SELECT ON trellis.turn_claims FROM authenticated;

-- Keep the original importer private for the audited wrapper below. Function
-- grants survive a rename, so revoke its old authenticated grant explicitly.
ALTER FUNCTION trellis_private.import_snapshot(jsonb) RENAME TO import_snapshot_v1;
REVOKE ALL ON FUNCTION trellis_private.import_snapshot_v1(jsonb)
    FROM PUBLIC, anon, authenticated;

CREATE FUNCTION trellis_private.import_snapshot(
    p_snapshot jsonb, p_capability text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_table text := p_snapshot->>'table';
    v_rows jsonb := p_snapshot->'rows';
    v_row jsonb;
    v_key text;
    v_column text;
    v_allowed text[];
    v_digest text;
    v_capability_digest text;
    v_saved_digest text;
    v_sealed boolean;
    v_chat_id uuid;
    v_run_id uuid;
    v_run trellis.runs%ROWTYPE;
    v_profile trellis.profiles%ROWTYPE;
    v_settings trellis.user_settings%ROWTYPE;
    v_progress trellis.onboarding_progress%ROWTYPE;
    v_sequence integer;
    v_previous_sequence integer;
    v_event_type text;
    v_expected_terminal_event text;
    v_result jsonb;
    v_inserted integer := 0;
    v_existing integer := 0;
    v_repeated integer := 0;
BEGIN
    IF p_snapshot IS NULL OR pg_catalog.jsonb_typeof(p_snapshot) <> 'object'
       OR v_table IS NULL OR v_rows IS NULL
       OR pg_catalog.jsonb_typeof(v_rows) <> 'array'
       OR pg_catalog.jsonb_array_length(v_rows) > 100
       OR pg_catalog.octet_length(p_snapshot::text) > 8388608 THEN
        RAISE EXCEPTION 'invalid import batch' USING ERRCODE = '22023';
    END IF;

    -- The lock serializes import, sealing, account bootstrap, and ordinary
    -- account mutations. It never takes a run lock before a chat lock.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('trellis-import:' || v_user::text, 0));
    v_capability_digest := trellis_private.capability_digest(p_capability);
    SELECT payload_hash INTO v_saved_digest
    FROM trellis_private.import_ledger
    WHERE user_id = v_user AND table_name = '__started__'
      AND row_key = 'account';
    IF FOUND THEN
        IF v_saved_digest <> v_capability_digest THEN
            RAISE EXCEPTION 'legacy import capability invalid'
                USING ERRCODE = '42501';
        END IF;
    ELSE
        IF v_table IS DISTINCT FROM 'profiles'
           OR pg_catalog.jsonb_array_length(v_rows) = 0 THEN
            RAISE EXCEPTION 'legacy import must begin with profile rows'
                USING ERRCODE = '42501';
        END IF;
        IF EXISTS (SELECT 1 FROM trellis.runs
            WHERE user_id = v_user AND status IN
                ('queued','running','waiting_for_approval','cancelling'))
           OR EXISTS (SELECT 1 FROM trellis.turn_claims
            WHERE user_id = v_user
              AND claimed_at > pg_catalog.clock_timestamp() - interval '5 minutes') THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'account_activity_busy';
        END IF;
        INSERT INTO trellis_private.import_ledger (
            user_id, table_name, row_key, payload_hash
        ) VALUES (v_user, '__started__', 'account', v_capability_digest);
    END IF;
    SELECT EXISTS (SELECT 1 FROM trellis_private.import_ledger
        WHERE user_id = v_user AND table_name = '__sealed__'
          AND row_key = 'account') INTO v_sealed;

    FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(v_rows) LOOP
        IF pg_catalog.jsonb_typeof(v_row) <> 'object' THEN
            RAISE EXCEPTION 'import row must be an object' USING ERRCODE = '22023';
        END IF;
        IF (CASE WHEN v_table = 'profiles' THEN v_row->>'id'
                 ELSE v_row->>'user_id' END) IS DISTINCT FROM v_user::text THEN
            RAISE EXCEPTION 'import owner differs from access token'
                USING ERRCODE = '42501';
        END IF;
        v_key := CASE
            WHEN v_table = 'run_events' THEN
                (v_row->>'run_id') || ':' || (v_row->>'sequence')
            WHEN v_table IN ('user_settings','onboarding_progress') THEN
                v_row->>'user_id'
            ELSE v_row->>'id' END;
        IF v_key IS NULL OR v_key = '' THEN
            RAISE EXCEPTION 'import primary key missing' USING ERRCODE = '22023';
        END IF;
        v_digest := pg_catalog.encode(pg_catalog.sha256(
            pg_catalog.convert_to(v_row::text, 'UTF8')), 'hex');
        SELECT payload_hash INTO v_saved_digest
        FROM trellis_private.import_ledger
        WHERE user_id = v_user AND table_name = v_table AND row_key = v_key;
        IF FOUND THEN
            IF v_saved_digest <> v_digest THEN
                RAISE EXCEPTION 'import source row changed after prior import'
                    USING ERRCODE = '23505';
            END IF;
            v_repeated := v_repeated + 1;
            CONTINUE;
        END IF;
        IF v_sealed THEN
            RAISE EXCEPTION 'legacy import is sealed' USING ERRCODE = '42501';
        END IF;
        IF v_table = 'runs' AND (
            v_row->>'status' NOT IN ('completed','failed','cancelled','interrupted')
            OR v_row->>'lease_token' IS NOT NULL
            OR v_row->>'lease_expires_at' IS NOT NULL
            OR v_row->>'waiting_tool_call_id' IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'cannot import active run or lease'
                USING ERRCODE = '22023';
        END IF;

        IF v_table IN ('profiles','user_settings','onboarding_progress') THEN
            -- A new computer may have called ensure_account before the old
            -- computer signs in. Merge only untouched generated defaults. A
            -- cloud edit always wins; the source hash is still ledgered so a
            -- retry cannot apply it later.
            CASE v_table
                WHEN 'profiles' THEN
                    v_allowed := ARRAY['id','display_name','created_at','updated_at'];
                WHEN 'user_settings' THEN
                    v_allowed := ARRAY['user_id','selected_provider','selected_model_id',
                                       'default_budget_preset','created_at','updated_at'];
                ELSE
                    v_allowed := ARRAY['user_id','flow_version','current_step',
                                       'completed_at','created_at','updated_at'];
            END CASE;
            FOR v_column IN SELECT key FROM pg_catalog.jsonb_object_keys(v_row) AS key LOOP
                IF NOT v_column = ANY(v_allowed) THEN
                    RAISE EXCEPTION 'import column not allowlisted: %', v_column
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;
            IF NOT (v_row ? 'created_at' AND v_row ? 'updated_at')
               OR (v_row->>'created_at')::timestamptz IS NULL
               OR (v_row->>'updated_at')::timestamptz IS NULL THEN
                RAISE EXCEPTION 'import timestamps missing' USING ERRCODE = '22023';
            END IF;
            IF v_table = 'profiles' THEN
                SELECT * INTO v_profile FROM trellis.profiles
                    WHERE id = v_user FOR UPDATE;
                IF FOUND THEN
                    IF v_profile.display_name IS NULL
                       AND v_profile.created_at = v_profile.updated_at THEN
                        UPDATE trellis.profiles SET
                            display_name = v_row->>'display_name',
                            created_at = (v_row->>'created_at')::timestamptz
                        WHERE id = v_user;
                    END IF;
                END IF;
            ELSIF v_table = 'user_settings' THEN
                IF v_row->>'selected_provider' IS NULL
                   OR v_row->>'selected_model_id' IS NULL
                   OR v_row->>'default_budget_preset' IS NULL
                   OR NOT EXISTS (SELECT 1 FROM trellis.models
                    WHERE id = v_row->>'selected_model_id'
                      AND provider_id = v_row->>'selected_provider')
                   OR v_row->>'default_budget_preset' NOT IN ('conservative','longer') THEN
                    RAISE EXCEPTION 'invalid imported settings' USING ERRCODE = '22023';
                END IF;
                SELECT * INTO v_settings FROM trellis.user_settings
                    WHERE user_id = v_user FOR UPDATE;
                IF FOUND THEN
                    IF v_settings.selected_provider = 'openai'
                       AND v_settings.selected_model_id = 'openai:gpt-5.5'
                       AND v_settings.default_budget_preset = 'conservative'
                       AND v_settings.created_at = v_settings.updated_at THEN
                        UPDATE trellis.user_settings SET
                            selected_provider = v_row->>'selected_provider',
                            selected_model_id = v_row->>'selected_model_id',
                            default_budget_preset = v_row->>'default_budget_preset',
                            created_at = (v_row->>'created_at')::timestamptz
                        WHERE user_id = v_user;
                    END IF;
                END IF;
            ELSE
                IF v_row->>'flow_version' IS NULL
                   OR v_row->>'current_step' IS NULL
                   OR (v_row->>'flow_version')::integer <= 0
                   OR v_row->>'current_step' NOT IN
                      ('intro','profile','model','complete')
                   OR ((v_row->>'current_step' = 'complete') IS DISTINCT FROM
                       (v_row->>'completed_at' IS NOT NULL)) THEN
                    RAISE EXCEPTION 'invalid imported onboarding progress'
                        USING ERRCODE = '22023';
                END IF;
                SELECT * INTO v_progress FROM trellis.onboarding_progress
                    WHERE user_id = v_user FOR UPDATE;
                IF FOUND THEN
                    IF v_progress.flow_version = 1
                       AND v_progress.current_step = 'intro'
                       AND v_progress.completed_at IS NULL
                       AND v_progress.created_at = v_progress.updated_at THEN
                        UPDATE trellis.onboarding_progress SET
                            flow_version = (v_row->>'flow_version')::integer,
                            current_step = v_row->>'current_step',
                            completed_at = (v_row->>'completed_at')::timestamptz,
                            created_at = (v_row->>'created_at')::timestamptz
                        WHERE user_id = v_user;
                    END IF;
                END IF;
            END IF;
            IF FOUND THEN
                INSERT INTO trellis_private.import_ledger (
                    user_id, table_name, row_key, payload_hash
                ) VALUES (v_user, v_table, v_key, v_digest);
                v_existing := v_existing + 1;
                CONTINUE;
            END IF;
        END IF;

        IF v_table = 'chats' THEN
            IF EXISTS (SELECT 1 FROM trellis.chats
                       WHERE id = (v_row->>'id')::uuid) THEN
                RAISE EXCEPTION 'import chat conflicts with cloud data'
                    USING ERRCODE = '23505';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM trellis_private.import_ledger
                WHERE user_id = v_user AND table_name = 'profiles'
                  AND row_key = v_user::text) THEN
                RAISE EXCEPTION 'import profile must precede chats'
                    USING ERRCODE = '42501';
            END IF;
        ELSIF v_table IN ('messages','runs') THEN
            v_chat_id := (v_row->>'chat_id')::uuid;
        ELSIF v_table = 'message_run_links' THEN
            SELECT chat_id INTO v_chat_id FROM trellis.messages
                WHERE user_id = v_user AND id = (v_row->>'id')::uuid;
            v_run_id := (v_row->>'run_id')::uuid;
        ELSIF v_table IN ('model_calls','run_events','run_messages','tool_calls') THEN
            v_run_id := (v_row->>'run_id')::uuid;
        END IF;

        IF v_run_id IS NOT NULL THEN
            SELECT * INTO v_run FROM trellis.runs
                WHERE id = v_run_id AND user_id = v_user;
            IF NOT FOUND OR v_run.status NOT IN
               ('completed','failed','cancelled','interrupted')
               OR NOT EXISTS (SELECT 1 FROM trellis_private.import_ledger
                   WHERE user_id = v_user AND table_name = 'runs'
                     AND row_key = v_run_id::text) THEN
                RAISE EXCEPTION 'import cannot change an active or cloud run'
                    USING ERRCODE = '42501';
            END IF;
            IF v_chat_id IS NULL THEN v_chat_id := v_run.chat_id; END IF;
        END IF;
        IF v_chat_id IS NOT NULL THEN
            PERFORM 1 FROM trellis.chats
                WHERE id = v_chat_id AND user_id = v_user FOR UPDATE;
            IF NOT FOUND OR NOT EXISTS (
                SELECT 1 FROM trellis_private.import_ledger
                WHERE user_id = v_user AND table_name = 'chats'
                  AND row_key = v_chat_id::text) THEN
                RAISE EXCEPTION 'import chat is not part of this legacy snapshot'
                    USING ERRCODE = '42501';
            END IF;
            IF EXISTS (SELECT 1 FROM trellis.runs
                WHERE user_id = v_user AND chat_id = v_chat_id
                  AND status IN ('queued','running','waiting_for_approval','cancelling')) THEN
                RAISE EXCEPTION 'import cannot change an active chat'
                    USING ERRCODE = '42501';
            END IF;
        END IF;

        IF v_table = 'runs' THEN
            IF EXISTS (SELECT 1 FROM trellis.runs
                       WHERE id = (v_row->>'id')::uuid) THEN
                RAISE EXCEPTION 'import run conflicts with cloud data'
                    USING ERRCODE = '23505';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM trellis_private.import_ledger
                WHERE user_id = v_user AND table_name = 'messages'
                  AND row_key = v_row->>'input_message_id')
               OR (v_row->>'retry_of' IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM trellis_private.import_ledger
                   WHERE user_id = v_user AND table_name = 'runs'
                     AND row_key = v_row->>'retry_of')) THEN
                RAISE EXCEPTION 'import run parents are not imported'
                    USING ERRCODE = '42501';
            END IF;
        ELSIF v_table = 'message_run_links' THEN
            IF NOT EXISTS (SELECT 1 FROM trellis_private.import_ledger
                WHERE user_id = v_user AND table_name = 'messages'
                  AND row_key = v_row->>'id') THEN
                RAISE EXCEPTION 'import cannot link a cloud message'
                    USING ERRCODE = '42501';
            END IF;
        ELSIF v_table = 'run_messages' THEN
            IF (v_row->>'role' = 'assistant' AND NOT EXISTS (
                SELECT 1 FROM trellis_private.import_ledger
                WHERE user_id = v_user AND table_name = 'model_calls'
                  AND row_key = v_row->>'model_call_id'))
               OR (v_row->>'role' = 'tool' AND NOT EXISTS (
                SELECT 1 FROM trellis_private.import_ledger
                WHERE user_id = v_user AND table_name = 'tool_calls'
                  AND row_key = v_row->>'tool_call_id')) THEN
                RAISE EXCEPTION 'import run message parent is not imported'
                    USING ERRCODE = '42501';
            END IF;
        ELSIF v_table = 'tool_calls' THEN
            IF NOT EXISTS (SELECT 1 FROM trellis_private.import_ledger
                WHERE user_id = v_user AND table_name = 'run_messages'
                  AND row_key = v_row->>'assistant_message_id') THEN
                RAISE EXCEPTION 'import tool call parent is not imported'
                    USING ERRCODE = '42501';
            END IF;
        ELSIF v_table = 'run_events' THEN
            v_sequence := (v_row->>'sequence')::integer;
            SELECT COALESCE(MAX(sequence), 0) INTO v_previous_sequence
                FROM trellis.run_events
                WHERE user_id = v_user AND run_id = v_run_id;
            IF v_sequence <> v_previous_sequence + 1
               OR v_sequence > v_run.last_event_sequence THEN
                RAISE EXCEPTION 'import run event sequence differs from run cursor'
                    USING ERRCODE = '22023';
            END IF;
            v_event_type := v_row->>'event_type';
            v_expected_terminal_event := 'run.' || v_run.status;
            IF (v_sequence = v_run.last_event_sequence
                  AND v_event_type IS DISTINCT FROM v_expected_terminal_event)
               OR (v_sequence < v_run.last_event_sequence
                  AND v_event_type IN ('run.completed','run.failed',
                      'run.cancelled','run.interrupted')) THEN
                RAISE EXCEPTION 'import terminal event differs from run status'
                    USING ERRCODE = '22023';
            END IF;
        END IF;

        v_result := trellis_private.import_snapshot_v1(
            pg_catalog.jsonb_build_object('table', v_table,
                                          'rows', pg_catalog.jsonb_build_array(v_row)));
        v_inserted := v_inserted + (v_result->>'inserted')::integer;
        v_existing := v_existing + (v_result->>'existing')::integer;
        v_repeated := v_repeated + (v_result->>'already_imported')::integer;
        v_chat_id := NULL;
        v_run_id := NULL;
    END LOOP;

    -- An empty batch still gets the original allowlist validation.
    IF pg_catalog.jsonb_array_length(v_rows) = 0 THEN
        PERFORM trellis_private.import_snapshot_v1(p_snapshot);
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'inserted', v_inserted, 'existing', v_existing,
        'already_imported', v_repeated);
END
$$;

DROP FUNCTION trellis.import_snapshot(jsonb);
CREATE FUNCTION trellis.import_snapshot(
    p_snapshot jsonb, p_capability text
) RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT trellis_private.import_snapshot(p_snapshot, p_capability)
$$;

CREATE FUNCTION trellis_private.seal_import(p_capability text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_run trellis.runs%ROWTYPE;
    v_count integer;
    v_max_sequence integer;
    v_final_type text;
    v_saved_digest text;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('trellis-import:' || v_user::text, 0));
    SELECT payload_hash INTO v_saved_digest
    FROM trellis_private.import_ledger
    WHERE user_id = v_user AND table_name = '__started__'
      AND row_key = 'account';
    IF NOT FOUND OR v_saved_digest <> trellis_private.capability_digest(p_capability) THEN
        RAISE EXCEPTION 'legacy import capability invalid'
            USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM trellis_private.import_ledger
        WHERE user_id = v_user AND table_name = '__sealed__'
          AND row_key = 'account') THEN
        RETURN '{"sealed":true}'::jsonb;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM trellis_private.import_ledger
        WHERE user_id = v_user AND table_name NOT IN ('__started__','__sealed__')) THEN
        RAISE EXCEPTION 'no legacy rows imported' USING ERRCODE = '22023';
    END IF;
    FOR v_run IN SELECT r.* FROM trellis.runs AS r
        JOIN trellis_private.import_ledger AS l
          ON l.user_id = r.user_id AND l.table_name = 'runs'
         AND l.row_key = r.id::text
        WHERE r.user_id = v_user LOOP
        SELECT count(*), COALESCE(MAX(sequence), 0)
            INTO v_count, v_max_sequence
        FROM trellis.run_events WHERE user_id = v_user AND run_id = v_run.id;
        SELECT event_type INTO v_final_type FROM trellis.run_events
            WHERE user_id = v_user AND run_id = v_run.id
              AND sequence = v_run.last_event_sequence;
        IF v_run.status NOT IN ('completed','failed','cancelled','interrupted')
           OR v_run.last_event_sequence < 1
           OR v_count <> v_run.last_event_sequence
           OR v_max_sequence <> v_run.last_event_sequence
           OR v_final_type IS DISTINCT FROM 'run.' || v_run.status THEN
            RAISE EXCEPTION 'legacy run event history is incomplete'
                USING ERRCODE = '22023';
        END IF;
    END LOOP;
    INSERT INTO trellis_private.import_ledger (
        user_id, table_name, row_key, payload_hash
    ) VALUES (v_user, '__sealed__', 'account',
              pg_catalog.encode(pg_catalog.sha256(
                  pg_catalog.convert_to('trellis-import-sealed-v1', 'UTF8')), 'hex'));
    RETURN '{"sealed":true}'::jsonb;
END
$$;

CREATE FUNCTION trellis.seal_import(p_capability text) RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT trellis_private.seal_import(p_capability)
$$;

-- The old dispatcher remains callable only by its owner. Guard ordinary
-- visible-message writes against active runs before delegating to it.
ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_v1;
REVOKE ALL ON FUNCTION trellis_private.mutate_v1(text,jsonb,text)
    FROM PUBLIC, anon, authenticated;

CREATE FUNCTION trellis_private.mutate(
    p_action text, p_payload jsonb, p_capability text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_chat_id uuid;
    v_claim_hash text;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_payload IS NULL OR pg_catalog.jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'mutation payload must be an object' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('trellis-import:' || v_user::text, 0));
    IF EXISTS (SELECT 1 FROM trellis_private.import_ledger
        WHERE user_id = v_user AND table_name = '__started__'
          AND row_key = 'account') AND NOT EXISTS (
        SELECT 1 FROM trellis_private.import_ledger
        WHERE user_id = v_user AND table_name = '__sealed__'
          AND row_key = 'account') THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'account_import_in_progress';
    END IF;
    IF p_action IN ('claim_turn','release_turn','add_user_message',
                    'add_assistant_message','create_run') THEN
        v_chat_id := (p_payload->>'session_id')::uuid;
        PERFORM 1 FROM trellis.chats
            WHERE id = v_chat_id AND user_id = v_user FOR UPDATE;
        IF NOT FOUND THEN
            IF p_action = 'claim_turn' THEN RETURN 'false'::jsonb; END IF;
            IF p_action = 'release_turn' THEN RETURN 'null'::jsonb; END IF;
            RAISE EXCEPTION 'session not found' USING ERRCODE = '22023';
        END IF;
        IF p_action = 'create_run' THEN
            IF EXISTS (SELECT 1 FROM trellis.turn_claims
                WHERE user_id = v_user AND chat_id = v_chat_id
                  AND claimed_at > pg_catalog.clock_timestamp() - interval '5 minutes') THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'session_turn_busy';
            END IF;
        ELSIF p_action <> 'release_turn' THEN
            IF EXISTS (SELECT 1 FROM trellis.runs
                WHERE user_id = v_user AND chat_id = v_chat_id
                  AND status IN ('queued','running','waiting_for_approval','cancelling')) THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'session_run_busy';
            END IF;
        END IF;
    END IF;
    IF p_action = 'claim_turn' THEN
        v_claim_hash := trellis_private.capability_digest(p_capability);
        DELETE FROM trellis.turn_claims WHERE user_id = v_user
            AND claimed_at <= v_now - interval '5 minutes';
        INSERT INTO trellis.turn_claims (
            user_id, chat_id, turn_id, claimed_at, claim_token_hash
        ) VALUES (v_user, v_chat_id, p_payload->>'turn_id', v_now,
                  v_claim_hash)
        ON CONFLICT DO NOTHING;
        RETURN pg_catalog.to_jsonb(FOUND);
    ELSIF p_action = 'release_turn' THEN
        v_claim_hash := trellis_private.capability_digest(p_capability);
        DELETE FROM trellis.turn_claims WHERE user_id = v_user
            AND chat_id = v_chat_id AND turn_id = p_payload->>'turn_id'
            AND claim_token_hash = v_claim_hash;
        RETURN 'null'::jsonb;
    ELSIF p_action IN ('add_user_message','add_assistant_message') THEN
        v_claim_hash := trellis_private.capability_digest(p_capability);
        PERFORM 1 FROM trellis.turn_claims
            WHERE user_id = v_user AND chat_id = v_chat_id
              AND turn_id = p_payload->>'turn_id'
              AND claim_token_hash = v_claim_hash
              AND claimed_at > v_now - interval '5 minutes'
            FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'turn capability invalid or expired'
                USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN trellis_private.mutate_v1(p_action, p_payload, p_capability);
END
$$;

CREATE OR REPLACE FUNCTION trellis.mutate(
    p_action text, p_payload jsonb, p_capability text DEFAULT NULL
) RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT trellis_private.mutate(p_action, p_payload, p_capability)
$$;

-- Direct bootstrap calls also take the account lock, preventing the default
-- row insert from racing with the old computer's first import batch.
CREATE FUNCTION trellis_private.ensure_account_guarded() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('trellis-import:' || v_user::text, 0));
    RETURN trellis_private.ensure_account();
END
$$;

CREATE OR REPLACE FUNCTION trellis.ensure_account() RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT trellis_private.ensure_account_guarded()
$$;

REVOKE ALL ON FUNCTION trellis_private.import_snapshot(jsonb,text)
    FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.seal_import(text)
    FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text)
    FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.ensure_account_guarded()
    FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis_private.ensure_account()
    FROM authenticated;
REVOKE ALL ON FUNCTION trellis.import_snapshot(jsonb,text),
    trellis.seal_import(text), trellis.mutate(text,jsonb,text),
    trellis.ensure_account() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION trellis_private.import_snapshot(jsonb,text),
    trellis_private.seal_import(text),
    trellis_private.mutate(text,jsonb,text),
    trellis_private.ensure_account_guarded() TO authenticated;
GRANT EXECUTE ON FUNCTION trellis.import_snapshot(jsonb,text),
    trellis.seal_import(text), trellis.mutate(text,jsonb,text),
    trellis.ensure_account() TO authenticated;

COMMIT;
