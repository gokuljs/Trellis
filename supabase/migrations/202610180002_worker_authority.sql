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
CREATE FUNCTION trellis_private.worker_claim_result(p_job trellis_private.jobs,p_capability text)
RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path='' AS $$
    SELECT jsonb_build_object('job_id',p_job.id,'run_id',p_job.run_id,
        'worker_id',p_job.worker_id,'attempt_id',p_job.attempt_id,
        'capability',p_capability,'lease_expires_at',p_job.lease_expires_at)
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_claim_result(trellis_private.jobs,text)
    FROM PUBLIC,anon,authenticated,trellis_runtime_worker;

CREATE FUNCTION trellis_private.require_assignment(p_claim jsonb,p_allow_finished boolean DEFAULT false)
RETURNS trellis.runs LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_job trellis_private.jobs%ROWTYPE;
    v_attempt trellis_private.execution_attempts%ROWTYPE; v_now timestamptz:=clock_timestamp();
BEGIN
    PERFORM trellis_private.require_worker();
    SELECT * INTO v_run FROM trellis.runs WHERE id=(p_claim->>'run_id')::uuid FOR UPDATE;
    IF NOT FOUND OR v_run.execution_mode<>'durable' THEN
        RAISE EXCEPTION 'execution assignment invalid or expired' USING ERRCODE='42501'; END IF;
    SELECT * INTO v_job FROM trellis_private.jobs WHERE id=(p_claim->>'job_id')::uuid
        AND run_id=v_run.id AND user_id=v_run.user_id FOR UPDATE;
    SELECT * INTO v_attempt FROM trellis_private.execution_attempts
        WHERE id=(p_claim->>'attempt_id')::uuid AND job_id=v_job.id AND run_id=v_run.id
            AND user_id=v_run.user_id FOR UPDATE;
    IF v_job.id IS NULL OR v_attempt.id IS NULL
       OR v_job.worker_id IS DISTINCT FROM p_claim->>'worker_id'
       OR v_run.worker_id IS DISTINCT FROM v_job.worker_id
       OR v_attempt.worker_id IS DISTINCT FROM v_job.worker_id
       OR v_run.attempt_id IS DISTINCT FROM v_attempt.id
       OR v_job.attempt_id IS DISTINCT FROM v_attempt.id
       OR v_attempt.capability_digest IS DISTINCT FROM trellis_private.capability_digest(p_claim->>'capability')
       OR v_attempt.lease_expires_at<=v_now
       OR NOT ((v_job.status IN ('claimed','running') AND v_attempt.status=v_job.status
            AND v_run.lease_token=v_attempt.capability_digest
            AND v_job.lease_expires_at>v_now AND v_run.lease_expires_at>v_now
            AND ((v_job.status='claimed' AND v_run.status='queued')
                OR (v_job.status='running' AND v_run.status IN ('running','cancelling'))))
          OR (p_allow_finished AND v_job.status='finished' AND v_attempt.status='finished'
            AND v_run.status IN ('completed','failed','cancelled','interrupted'))) THEN
        RAISE EXCEPTION 'execution assignment invalid or expired' USING ERRCODE='42501';
    END IF;
    RETURN v_run;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.require_assignment(jsonb,boolean)
    FROM PUBLIC,anon,authenticated,trellis_runtime_worker;

CREATE FUNCTION trellis_private.worker_claim(p_worker_id text,p_attempt_id uuid,p_capability text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='2s' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_job trellis_private.jobs%ROWTYPE;
    v_attempt trellis_private.execution_attempts%ROWTYPE;
    v_digest text; v_expiry timestamptz;
BEGIN
    PERFORM trellis_private.require_worker();
    IF p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 200 OR p_attempt_id IS NULL THEN
        RAISE EXCEPTION 'invalid worker claim identity' USING ERRCODE='22023'; END IF;
    v_digest:=trellis_private.capability_digest(p_capability);
    PERFORM pg_advisory_xact_lock(hashtextextended('trellis-attempt:'||p_attempt_id::text,0));
    SELECT * INTO v_attempt FROM trellis_private.execution_attempts WHERE id=p_attempt_id;
    IF FOUND THEN
        SELECT * INTO v_job FROM trellis_private.jobs WHERE id=v_attempt.job_id;
        PERFORM trellis_private.require_assignment(jsonb_build_object('job_id',v_job.id,
            'run_id',v_job.run_id,'worker_id',p_worker_id,'attempt_id',p_attempt_id,'capability',p_capability));
        RETURN trellis_private.worker_claim_result(v_job,p_capability);
    END IF;
    SELECT r.* INTO v_run FROM trellis.runs r JOIN trellis_private.jobs j ON j.run_id=r.id
        WHERE j.status='ready' AND r.execution_mode='durable' AND r.status='queued'
            AND r.cancel_requested_at IS NULL
        ORDER BY j.created_at,j.id LIMIT 1 FOR UPDATE OF r SKIP LOCKED;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO v_job FROM trellis_private.jobs WHERE run_id=v_run.id AND status='ready' FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    v_expiry:=clock_timestamp()+interval '60 seconds';
    INSERT INTO trellis_private.execution_attempts(id,job_id,run_id,user_id,worker_id,status,
        capability_digest,lease_expires_at)
    VALUES(p_attempt_id,v_job.id,v_run.id,v_run.user_id,p_worker_id,'claimed',v_digest,v_expiry);
    UPDATE trellis.runs SET worker_id=p_worker_id,attempt_id=p_attempt_id,
        lease_token=v_digest,lease_expires_at=v_expiry WHERE id=v_run.id;
    UPDATE trellis_private.jobs SET status='claimed',worker_id=p_worker_id,
        attempt_id=p_attempt_id,lease_expires_at=v_expiry,updated_at=clock_timestamp()
        WHERE id=v_job.id RETURNING * INTO v_job;
    RETURN trellis_private.worker_claim_result(v_job,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_claim(text,uuid,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_claim(text,uuid,text) TO trellis_runtime_worker;

CREATE FUNCTION trellis_private.worker_begin(p_claim jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='2s' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_now timestamptz;
BEGIN
    v_run:=trellis_private.require_assignment(p_claim);
    IF v_run.status='queued' THEN
        v_now:=clock_timestamp();
        UPDATE trellis.runs SET status='running',started_at=v_now,
            deadline_at=v_now+make_interval(secs=>max_seconds) WHERE id=v_run.id;
        UPDATE trellis_private.jobs SET status='running',updated_at=v_now
            WHERE id=(p_claim->>'job_id')::uuid;
        UPDATE trellis_private.execution_attempts SET status='running',started_at=v_now
            WHERE id=(p_claim->>'attempt_id')::uuid;
        PERFORM trellis_private.append_event(v_run.user_id,v_run.id,'run.started','{}'::jsonb);
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id;
    RETURN to_jsonb(v_run)-'lease_token';
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_begin(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_begin(jsonb) TO trellis_runtime_worker;
COMMIT;
