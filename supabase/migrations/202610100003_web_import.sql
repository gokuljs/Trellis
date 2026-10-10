-- Additive, per-account import from the PR1 account-scoped SQLite files.
-- The private ledger records source row hashes so retries do not overwrite
-- changes made in Supabase after the first successful import.
BEGIN;

CREATE TABLE trellis_private.import_ledger (
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    table_name text NOT NULL,
    row_key text NOT NULL,
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, table_name, row_key)
);
ALTER TABLE trellis_private.import_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE trellis_private.import_ledger FORCE ROW LEVEL SECURITY;
REVOKE ALL ON trellis_private.import_ledger FROM PUBLIC, anon, authenticated;
CREATE TRIGGER touch_updated_at BEFORE UPDATE ON trellis_private.import_ledger
FOR EACH ROW EXECUTE FUNCTION trellis_private.touch_updated_at();

CREATE FUNCTION trellis_private.import_snapshot(p_snapshot jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_table text := p_snapshot->>'table';
    v_rows jsonb := p_snapshot->'rows';
    v_allowed text[];
    v_row jsonb;
    v_normalized jsonb;
    v_existing jsonb;
    v_key text;
    v_row_key text;
    v_digest text;
    v_saved_digest text;
    v_columns text;
    v_select_columns text;
    v_inserted integer;
    v_insert_count integer := 0;
    v_existing_count integer := 0;
    v_repeated_count integer := 0;
    v_message trellis.messages%ROWTYPE;
    v_run trellis.runs%ROWTYPE;
BEGIN
    IF p_snapshot IS NULL OR pg_catalog.jsonb_typeof(p_snapshot) <> 'object'
       OR pg_catalog.jsonb_typeof(v_rows) <> 'array'
       OR pg_catalog.jsonb_array_length(v_rows) > 100
       OR pg_catalog.octet_length(p_snapshot::text) > 8388608 THEN
        RAISE EXCEPTION 'invalid import batch' USING ERRCODE = '22023';
    END IF;

    CASE v_table
      WHEN 'profiles' THEN
        v_allowed := ARRAY['id','display_name','created_at','updated_at'];
      WHEN 'user_settings' THEN
        v_allowed := ARRAY['user_id','selected_provider','selected_model_id',
                           'default_budget_preset','created_at','updated_at'];
      WHEN 'onboarding_progress' THEN
        v_allowed := ARRAY['user_id','flow_version','current_step','completed_at',
                           'created_at','updated_at'];
      WHEN 'chats' THEN
        v_allowed := ARRAY['id','user_id','title','workspace_path',
                           'created_at','updated_at'];
      WHEN 'messages' THEN
        v_allowed := ARRAY['id','user_id','chat_id','turn_id','ordinal','role',
                           'content','provider','model','run_id',
                           'created_at','updated_at'];
      WHEN 'runs' THEN
        v_allowed := ARRAY['id','user_id','chat_id','turn_id','input_message_id',
                           'client_request_id','retry_of','status','provider_id',
                           'model_id','adapter_kind','upstream_model_id',
                           'budget_preset','max_model_calls','max_tool_calls',
                           'max_total_tokens','max_cost_usd','deadline_at',
                           'cancel_requested_at','lease_token','lease_expires_at',
                           'recovery_count','waiting_tool_call_id','stop_reason',
                           'error_code','error_message','last_event_sequence',
                           'started_at','finished_at','created_at','updated_at'];
      WHEN 'model_calls' THEN
        v_allowed := ARRAY['id','user_id','run_id','step_index','provider_id',
                           'model_id','adapter_kind','status','request_snapshot',
                           'response_snapshot','provider_response_id','finish_reason',
                           'input_tokens','output_tokens','reasoning_tokens',
                           'cached_read_tokens','cache_creation_tokens',
                           'estimated_cost','error_code','error_message',
                           'started_at','finished_at','created_at','updated_at'];
      WHEN 'run_events' THEN
        v_allowed := ARRAY['user_id','run_id','sequence','event_type',
                           'event_version','data','created_at','updated_at'];
      WHEN 'run_messages' THEN
        v_allowed := ARRAY['id','user_id','run_id','ordinal','role','content',
                           'continuation','model_call_id','tool_call_id',
                           'created_at','updated_at'];
      WHEN 'tool_calls' THEN
        v_allowed := ARRAY['id','user_id','run_id','assistant_message_id',
                           'call_index','provider_call_id','name','arguments',
                           'status','approval_decision','approval_decided_at',
                           'approval_preview','finished_at','created_at','updated_at'];
      WHEN 'message_run_links' THEN
        v_allowed := ARRAY['user_id','id','run_id'];
      ELSE
        RAISE EXCEPTION 'import table not allowlisted' USING ERRCODE = '22023';
    END CASE;

    FOR v_row IN SELECT value FROM pg_catalog.jsonb_array_elements(v_rows) LOOP
        IF pg_catalog.jsonb_typeof(v_row) <> 'object' THEN
            RAISE EXCEPTION 'import row must be an object' USING ERRCODE = '22023';
        END IF;
        FOR v_key IN SELECT key FROM pg_catalog.jsonb_object_keys(v_row) AS key LOOP
            IF NOT v_key = ANY(v_allowed) THEN
                RAISE EXCEPTION 'import column not allowlisted: %', v_key
                    USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF (CASE WHEN v_table = 'profiles' THEN v_row->>'id'
                 ELSE v_row->>'user_id' END) IS DISTINCT FROM v_user::text THEN
            RAISE EXCEPTION 'import owner differs from access token'
                USING ERRCODE = '42501';
        END IF;
        IF v_table = 'run_events' THEN
            v_row_key := (v_row->>'run_id') || ':' || (v_row->>'sequence');
        ELSE
            v_row_key := CASE WHEN v_table IN ('user_settings','onboarding_progress')
                THEN v_row->>'user_id' ELSE v_row->>'id' END;
        END IF;
        IF v_row_key IS NULL OR v_row_key = '' THEN
            RAISE EXCEPTION 'import primary key missing' USING ERRCODE = '22023';
        END IF;
        IF v_table = 'runs' AND (
            v_row->>'status' NOT IN ('completed','failed','cancelled','interrupted')
            OR v_row->>'lease_token' IS NOT NULL
            OR v_row->>'lease_expires_at' IS NOT NULL
            OR v_row->>'waiting_tool_call_id' IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'cannot import active run or lease' USING ERRCODE = '22023';
        END IF;
        IF v_table = 'model_calls' AND v_row->>'status' NOT IN
            ('completed','failed','cancelled','timed_out') THEN
            RAISE EXCEPTION 'cannot import active model call' USING ERRCODE = '22023';
        END IF;
        IF v_table = 'tool_calls' AND v_row->>'status' NOT IN
            ('completed','failed','denied','cancelled','timed_out') THEN
            RAISE EXCEPTION 'cannot import active tool call' USING ERRCODE = '22023';
        END IF;
        IF v_table = 'messages' AND v_row->>'run_id' IS NOT NULL THEN
            RAISE EXCEPTION 'message run links must import separately'
                USING ERRCODE = '22023';
        END IF;

        v_digest := pg_catalog.encode(pg_catalog.sha256(
            pg_catalog.convert_to(v_row::text, 'UTF8')), 'hex');
        SELECT payload_hash INTO v_saved_digest FROM trellis_private.import_ledger
        WHERE user_id = v_user AND table_name = v_table AND row_key = v_row_key;
        IF FOUND THEN
            IF v_saved_digest <> v_digest THEN
                RAISE EXCEPTION 'import source row changed after prior import'
                    USING ERRCODE = '23505';
            END IF;
            v_repeated_count := v_repeated_count + 1;
            CONTINUE;
        END IF;

        IF v_table = 'message_run_links' THEN
            SELECT * INTO v_message FROM trellis.messages
            WHERE id = (v_row->>'id')::uuid AND user_id = v_user FOR UPDATE;
            SELECT * INTO v_run FROM trellis.runs
            WHERE id = (v_row->>'run_id')::uuid AND user_id = v_user;
            IF v_message.id IS NULL OR v_run.id IS NULL
               OR v_message.chat_id <> v_run.chat_id
               OR v_message.turn_id <> v_run.turn_id
               OR (v_message.run_id IS NOT NULL AND v_message.run_id <> v_run.id) THEN
                RAISE EXCEPTION 'message run link conflicts with existing data'
                    USING ERRCODE = '23505';
            END IF;
            IF v_message.run_id IS NULL THEN
                UPDATE trellis.messages SET run_id = v_run.id
                WHERE id = v_message.id AND user_id = v_user;
                v_insert_count := v_insert_count + 1;
            ELSE
                v_existing_count := v_existing_count + 1;
            END IF;
        ELSE
            SELECT pg_catalog.string_agg(pg_catalog.format('%I', key), ', ' ORDER BY key),
                   pg_catalog.string_agg(pg_catalog.format('x.%I', key), ', ' ORDER BY key)
            INTO v_columns, v_select_columns
            FROM pg_catalog.jsonb_object_keys(v_row) AS key;
            IF v_columns IS NULL THEN
                RAISE EXCEPTION 'empty import row' USING ERRCODE = '22023';
            END IF;
            EXECUTE pg_catalog.format(
                'SELECT pg_catalog.to_jsonb(x) FROM pg_catalog.jsonb_populate_record('
                || 'NULL::trellis.%I, $1) AS x', v_table)
            INTO v_normalized USING v_row;
            EXECUTE pg_catalog.format(
                'INSERT INTO trellis.%I (%s) SELECT %s FROM '
                || 'pg_catalog.jsonb_populate_record(NULL::trellis.%I, $1) AS x '
                || 'ON CONFLICT DO NOTHING RETURNING 1',
                v_table, v_columns, v_select_columns, v_table)
            INTO v_inserted USING v_row;
            IF v_inserted IS NOT NULL THEN
                v_insert_count := v_insert_count + 1;
            ELSE
                IF v_table = 'run_events' THEN
                    EXECUTE 'SELECT pg_catalog.to_jsonb(t) FROM trellis.run_events t '
                        || 'WHERE user_id=($1->>''user_id'')::uuid '
                        || 'AND run_id=($1->>''run_id'')::uuid '
                        || 'AND sequence=($1->>''sequence'')::integer'
                    INTO v_existing USING v_row;
                ELSIF v_table IN ('user_settings','onboarding_progress') THEN
                    EXECUTE pg_catalog.format(
                        'SELECT pg_catalog.to_jsonb(t) FROM trellis.%I t '
                        || 'WHERE user_id=($1->>''user_id'')::uuid', v_table)
                    INTO v_existing USING v_row;
                ELSE
                    EXECUTE pg_catalog.format(
                        'SELECT pg_catalog.to_jsonb(t) FROM trellis.%I t '
                        || 'WHERE id=($1->>''id'')::uuid', v_table)
                    INTO v_existing USING v_row;
                END IF;
                IF v_existing IS NULL OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_object_keys(v_row) AS key
                    WHERE v_existing->key IS DISTINCT FROM v_normalized->key
                ) THEN
                    RAISE EXCEPTION 'import row conflicts with existing data'
                        USING ERRCODE = '23505';
                END IF;
                v_existing_count := v_existing_count + 1;
            END IF;
        END IF;

        INSERT INTO trellis_private.import_ledger (
            user_id, table_name, row_key, payload_hash
        ) VALUES (v_user, v_table, v_row_key, v_digest)
        ON CONFLICT DO NOTHING;
        SELECT payload_hash INTO v_saved_digest FROM trellis_private.import_ledger
        WHERE user_id = v_user AND table_name = v_table AND row_key = v_row_key;
        IF v_saved_digest <> v_digest THEN
            RAISE EXCEPTION 'concurrent import source conflict' USING ERRCODE = '23505';
        END IF;
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'inserted', v_insert_count, 'existing', v_existing_count,
        'already_imported', v_repeated_count);
END
$$;

CREATE FUNCTION trellis.import_snapshot(p_snapshot jsonb) RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT trellis_private.import_snapshot(p_snapshot)
$$;

REVOKE ALL ON FUNCTION trellis_private.import_snapshot(jsonb) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION trellis.import_snapshot(jsonb) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION trellis_private.import_snapshot(jsonb) TO authenticated;
GRANT EXECUTE ON FUNCTION trellis.import_snapshot(jsonb) TO authenticated;

COMMIT;
