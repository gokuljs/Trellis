BEGIN;

CREATE FUNCTION trellis.readiness() RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
    SELECT pg_catalog.jsonb_build_object('status', CASE WHEN
        pg_catalog.to_regclass('trellis.runs') IS NOT NULL
        AND pg_catalog.to_regclass('trellis.run_events') IS NOT NULL
        AND pg_catalog.to_regclass('trellis.model_calls') IS NOT NULL
        AND pg_catalog.to_regprocedure('trellis.ensure_account()') IS NOT NULL
        AND pg_catalog.to_regprocedure(
            'trellis.read_session_run_summaries(uuid,integer,integer,text,boolean)'
        ) IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM pg_catalog.pg_attribute
            WHERE attrelid=pg_catalog.to_regclass('trellis.model_calls')
                AND attname='accounted_total_tokens' AND NOT attisdropped
        )
        THEN 'ready' ELSE 'unavailable' END)
$$;

REVOKE ALL ON FUNCTION trellis.readiness() FROM PUBLIC;
GRANT USAGE ON SCHEMA trellis TO anon;
GRANT EXECUTE ON FUNCTION trellis.readiness() TO anon, authenticated;

COMMIT;
