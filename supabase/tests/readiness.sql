BEGIN;

SET LOCAL ROLE anon;
DO $$
BEGIN
    ASSERT trellis.readiness() = '{"status":"ready"}'::jsonb;
    ASSERT NOT pg_catalog.has_table_privilege(current_user, 'trellis.runs', 'SELECT');
    ASSERT NOT pg_catalog.has_table_privilege(current_user, 'trellis.run_events', 'SELECT');
    ASSERT NOT pg_catalog.has_table_privilege(current_user, 'trellis.model_calls', 'SELECT');
    ASSERT NOT pg_catalog.has_function_privilege(current_user, 'trellis.ensure_account()', 'EXECUTE');
    ASSERT NOT pg_catalog.has_function_privilege(current_user, 'trellis.mutate(text,jsonb,text)', 'EXECUTE');
    ASSERT NOT pg_catalog.has_schema_privilege(current_user, 'trellis_private', 'USAGE');
END;
$$;

RESET ROLE;
ALTER TABLE trellis.model_calls RENAME COLUMN accounted_total_tokens TO temporary_missing_tokens;
SET LOCAL ROLE anon;
DO $$
BEGIN
    ASSERT trellis.readiness() = '{"status":"unavailable"}'::jsonb;
END;
$$;

ROLLBACK;
