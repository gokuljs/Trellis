\set ON_ERROR_STOP on

DO $check$
BEGIN
    IF has_table_privilege('authenticated', 'trellis_private.import_ledger', 'SELECT')
       OR has_table_privilege('authenticated', 'trellis_private.import_ledger', 'INSERT')
       OR has_table_privilege('authenticated', 'trellis_private.import_ledger', 'UPDATE') THEN
        RAISE EXCEPTION 'private import ledger accessible directly';
    END IF;
    IF has_function_privilege('anon', 'trellis.import_snapshot(jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'anon can call import RPC';
    END IF;
END
$check$;

INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000c3', 'carol@example.test'),
    ('00000000-0000-0000-0000-0000000000d4', 'dave@example.test');
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000c3', false);

DO $check$
DECLARE
    batch jsonb := pg_catalog.jsonb_build_object(
        'table', 'profiles', 'rows', pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'id', '00000000-0000-0000-0000-0000000000c3',
                'display_name', 'Carol',
                'created_at', '2026-10-01T00:00:00Z',
                'updated_at', '2026-10-01T00:00:00Z')));
    result jsonb;
BEGIN
    result := trellis.import_snapshot(batch);
    IF result->>'inserted' <> '1' THEN
        RAISE EXCEPTION 'profile import did not insert';
    END IF;
    PERFORM trellis.mutate('update_profile', '{"display_name":"Changed"}'::jsonb);
    result := trellis.import_snapshot(batch);
    IF result->>'already_imported' <> '1' THEN
        RAISE EXCEPTION 'repeat import was not idempotent';
    END IF;
    IF (SELECT display_name FROM trellis.profiles WHERE id=
        '00000000-0000-0000-0000-0000000000c3') <> 'Changed' THEN
        RAISE EXCEPTION 'repeat import overwrote cloud change';
    END IF;

    result := trellis.import_snapshot(pg_catalog.jsonb_build_object(
        'table', 'chats', 'rows', pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'id', '00000000-0000-0000-0000-0000000000c4',
                'user_id', '00000000-0000-0000-0000-0000000000c3',
                'title', pg_catalog.repeat('x', 300000),
                'created_at', '2026-10-01T00:00:00Z',
                'updated_at', '2026-10-01T00:00:00Z'))));
    IF result->>'inserted' <> '1' THEN
        RAISE EXCEPTION 'valid large legacy row was rejected';
    END IF;

    BEGIN
        PERFORM trellis.import_snapshot(pg_catalog.jsonb_build_object(
            'table', 'profiles', 'rows', pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'id', '00000000-0000-0000-0000-0000000000d4',
                    'created_at', '2026-10-01T00:00:00Z',
                    'updated_at', '2026-10-01T00:00:00Z'))));
        RAISE EXCEPTION 'cross-account import accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;

    BEGIN
        PERFORM trellis.import_snapshot('{"table":"workspace_test_presets","rows":[]}'::jsonb);
        RAISE EXCEPTION 'retired table import accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;

    BEGIN
        PERFORM trellis.import_snapshot(pg_catalog.jsonb_build_object(
            'table', 'runs', 'rows', pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'id', '00000000-0000-0000-0000-000000000010',
                    'user_id', '00000000-0000-0000-0000-0000000000c3',
                    'status', 'running'))));
        RAISE EXCEPTION 'active run import accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
END
$check$;

RESET ROLE;
