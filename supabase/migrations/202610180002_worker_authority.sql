-- Execution helpers take an owner explicitly. Only a validated worker assignment
-- or the authenticated browser boundary may select that owner.
BEGIN;
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='trellis_runtime_worker') THEN
        CREATE ROLE trellis_runtime_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 16;
    END IF;
END;
$role$;
GRANT USAGE ON SCHEMA trellis_private TO trellis_runtime_worker;
REVOKE ALL ON ALL TABLES IN SCHEMA trellis,trellis_private FROM trellis_runtime_worker;

CREATE FUNCTION trellis_private.require_worker() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF session_user<>'trellis_runtime_worker' THEN
        RAISE EXCEPTION 'restricted worker identity required' USING ERRCODE='42501';
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.require_worker() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.require_worker() TO trellis_runtime_worker;

CREATE FUNCTION trellis_private.require_run_owned(p_owner uuid,p_run_id uuid,p_capability text)
RETURNS trellis.runs LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_run trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO v_run FROM trellis.runs WHERE id=p_run_id AND user_id=p_owner FOR UPDATE;
    IF NOT FOUND OR v_run.lease_token IS NULL
       OR v_run.lease_token IS DISTINCT FROM trellis_private.capability_digest(p_capability)
       OR v_run.lease_expires_at IS NULL OR v_run.lease_expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'run capability invalid or expired' USING ERRCODE='42501';
    END IF;
    RETURN v_run;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.require_run_owned(uuid,uuid,text)
    FROM PUBLIC,anon,authenticated,trellis_runtime_worker;

-- Retain the reviewed execution implementations, including checkpoint and event
-- accounting. Clone their call graph with explicit owner parameters, without
-- setting authentication claims or granting helpers to either caller role.
DO $helpers$
DECLARE v_function record; v_callee record; v_definition text;
BEGIN
    FOR v_function IN SELECT p.oid,p.proname FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='trellis_private' AND p.proname LIKE 'mutate%'
          AND pg_get_function_identity_arguments(p.oid)='p_action text, p_payload jsonb, p_capability text'
    LOOP
        v_definition:=pg_get_functiondef(v_function.oid);
        v_definition:=replace(v_definition,'FUNCTION trellis_private.'||v_function.proname||'(',
            'FUNCTION trellis_private.'||v_function.proname||'_owned(p_owner uuid, ');
        v_definition:=replace(v_definition,'trellis_private.require_user()', 'p_owner');
        v_definition:=replace(v_definition,'auth.uid()', 'p_owner');
        v_definition:=replace(v_definition,'trellis_private.require_run(',
            'trellis_private.require_run_owned(p_owner,');
        FOR v_callee IN SELECT p.proname FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='trellis_private' AND p.proname LIKE 'mutate%'
              AND pg_get_function_identity_arguments(p.oid)='p_action text, p_payload jsonb, p_capability text'
        LOOP
            v_definition:=replace(v_definition,'trellis_private.'||v_callee.proname||'(',
                'trellis_private.'||v_callee.proname||'_owned(p_owner,');
        END LOOP;
        EXECUTE v_definition;
        EXECUTE format('REVOKE ALL ON FUNCTION trellis_private.%I(uuid,text,jsonb,text) FROM PUBLIC,anon,authenticated,trellis_runtime_worker',v_function.proname||'_owned');
    END LOOP;
END;
$helpers$;
COMMIT;
