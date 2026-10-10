\set ON_ERROR_STOP on

DO $check$
DECLARE
    missing text;
BEGIN
    SELECT string_agg(table_name, ', ') INTO missing
    FROM (
        SELECT unnest(ARRAY[
            'profiles', 'models', 'user_settings', 'onboarding_progress',
            'chats', 'messages', 'turn_claims', 'runs', 'model_calls',
            'run_events', 'run_messages', 'tool_calls'
        ]) AS table_name
    ) expected
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.tables t
        WHERE t.table_schema = 'trellis' AND t.table_name = expected.table_name
    );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'missing tables: %', missing;
    END IF;

    SELECT string_agg(t.table_name, ', ') INTO missing
    FROM information_schema.tables t
    WHERE t.table_schema = 'trellis'
      AND t.table_type = 'BASE TABLE'
      AND (
          NOT EXISTS (SELECT 1 FROM information_schema.columns c WHERE c.table_schema=t.table_schema AND c.table_name=t.table_name AND c.column_name='created_at' AND c.data_type='timestamp with time zone')
          OR NOT EXISTS (SELECT 1 FROM information_schema.columns c WHERE c.table_schema=t.table_schema AND c.table_name=t.table_name AND c.column_name='updated_at' AND c.data_type='timestamp with time zone')
      );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'tables without timestamptz timestamps: %', missing;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='trellis'
       AND table_name IN ('workspace_test_presets', 'installations', 'chat_workspaces')) THEN
        RAISE EXCEPTION 'retired/local-only tables found';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='trellis' AND c.relkind='r' AND NOT c.relrowsecurity
    ) THEN
        RAISE EXCEPTION 'RLS disabled on a trellis table';
    END IF;
END $check$;

DO $check$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid='trellis.messages'::regclass
          AND c.conname='messages_run_owner_fk'
          AND pg_get_constraintdef(c.oid) LIKE '%(user_id, chat_id, turn_id, run_id)%'
    ) THEN
        RAISE EXCEPTION 'message-run FK must match owner, chat and turn';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid='trellis.runs'::regclass
          AND c.conname='runs_input_message_owner_fk'
          AND pg_get_constraintdef(c.oid) LIKE '%(user_id, chat_id, turn_id, input_message_id)%'
    ) THEN
        RAISE EXCEPTION 'run-input FK must match owner, chat and turn';
    END IF;
END $check$;

DO $check$
DECLARE
    table_name text;
BEGIN
    IF has_schema_privilege('anon', 'trellis', 'USAGE') THEN
        RAISE EXCEPTION 'anon can use trellis schema';
    END IF;
    FOR table_name IN
        SELECT t.table_name FROM information_schema.tables t
        WHERE t.table_schema='trellis' AND t.table_type='BASE TABLE'
    LOOP
        IF has_table_privilege('authenticated', 'trellis.' || table_name, 'INSERT')
           OR has_table_privilege('authenticated', 'trellis.' || table_name, 'UPDATE')
           OR has_table_privilege('authenticated', 'trellis.' || table_name, 'DELETE') THEN
            RAISE EXCEPTION 'authenticated can write table directly: %', table_name;
        END IF;
    END LOOP;
    IF has_column_privilege('authenticated', 'trellis.runs', 'lease_token', 'SELECT') THEN
        RAISE EXCEPTION 'run capability digest selectable';
    END IF;
END $check$;
