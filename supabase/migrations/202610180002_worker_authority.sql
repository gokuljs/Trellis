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
        v_definition:=replace(v_definition,'PERFORM trellis_private.ensure_account();','PERFORM 1;');
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
CREATE TABLE trellis_private.worker_operations (
    attempt_id uuid NOT NULL REFERENCES trellis_private.execution_attempts(id) ON DELETE CASCADE,
    operation_id uuid NOT NULL,
    fingerprint text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY(attempt_id,operation_id)
);
REVOKE ALL ON trellis_private.worker_operations FROM PUBLIC,anon,authenticated,trellis_runtime_worker;

CREATE FUNCTION trellis_private.worker_mutate(p_claim jsonb,p_operation_id uuid,p_action text,p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='2s' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_result jsonb; v_prior trellis_private.worker_operations%ROWTYPE;
    v_fingerprint text; v_call_id uuid;
BEGIN
    v_run:=trellis_private.require_assignment(p_claim,true);
    IF p_operation_id IS NULL OR p_action IS NULL OR jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR octet_length(p_payload::text)>4194304 OR p_action NOT IN (
        'append_run_event','transition_run_record','complete_run','create_model_call',
        'update_model_call','record_assistant_message','append_assistant_batch',
        'finish_assistant_output','save_context_checkpoint') THEN
        RAISE EXCEPTION 'worker operation is not permitted' USING ERRCODE='42501'; END IF;
    v_fingerprint:=encode(sha256(convert_to(jsonb_build_array(p_action,p_payload)::text,'UTF8')),'hex');
    SELECT * INTO v_prior FROM trellis_private.worker_operations
        WHERE attempt_id=(p_claim->>'attempt_id')::uuid AND operation_id=p_operation_id;
    IF FOUND THEN
        IF v_prior.fingerprint<>v_fingerprint THEN
            RAISE EXCEPTION 'operation identity conflicts with saved content' USING ERRCODE='22023'; END IF;
        RETURN v_prior.result;
    END IF;
    IF v_run.status NOT IN ('running','cancelling') THEN
        RAISE EXCEPTION 'execution has not begun or is finished' USING ERRCODE='42501'; END IF;
    IF (p_payload ? 'run_id' AND p_payload->>'run_id' IS DISTINCT FROM v_run.id::text)
       OR (p_payload ? 'session_id' AND p_payload->>'session_id' IS DISTINCT FROM v_run.chat_id::text)
       OR (p_payload ? 'model_id' AND p_payload->>'model_id' IS DISTINCT FROM v_run.model_id) THEN
        RAISE EXCEPTION 'operation is outside execution assignment' USING ERRCODE='42501'; END IF;
    v_call_id:=COALESCE((p_payload->>'call_id')::uuid,(p_payload->>'model_call_id')::uuid);
    IF v_call_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM trellis.model_calls
        WHERE id=v_call_id AND run_id=v_run.id AND user_id=v_run.user_id) THEN
        RAISE EXCEPTION 'model call is outside execution assignment' USING ERRCODE='42501'; END IF;
    IF p_action='create_model_call' AND v_run.status<>'running' THEN
        RAISE EXCEPTION 'run cancellation requested' USING ERRCODE='42501'; END IF;
    IF p_action='transition_run_record' AND COALESCE(p_payload->>'next_status','') NOT IN
        ('completed','failed','cancelled','interrupted') THEN
        RAISE EXCEPTION 'worker cannot change execution ownership' USING ERRCODE='42501'; END IF;
    IF p_action='record_assistant_message' AND COALESCE(jsonb_array_length(p_payload->'message'->'tool_calls'),0)>0 THEN
        RAISE EXCEPTION 'durable tools are unavailable' USING ERRCODE='42501'; END IF;
    IF (SELECT count(*) FROM trellis_private.worker_operations
        WHERE attempt_id=(p_claim->>'attempt_id')::uuid)>=32768 THEN
        RAISE EXCEPTION 'worker operation limit reached' USING ERRCODE='22023'; END IF;
    v_result:=trellis_private.mutate_owned(v_run.user_id,p_action,
        p_payload||jsonb_build_object('run_id',v_run.id),p_claim->>'capability');
    SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id;
    IF v_run.status IN ('completed','failed','cancelled','interrupted') THEN
        UPDATE trellis_private.jobs SET status='finished',updated_at=clock_timestamp()
            WHERE id=(p_claim->>'job_id')::uuid;
        UPDATE trellis_private.execution_attempts SET status='finished',finished_at=clock_timestamp(),outcome=v_run.status
            WHERE id=(p_claim->>'attempt_id')::uuid;
    END IF;
    INSERT INTO trellis_private.worker_operations(attempt_id,operation_id,fingerprint,result)
        VALUES((p_claim->>'attempt_id')::uuid,p_operation_id,v_fingerprint,COALESCE(v_result,'null'::jsonb));
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_mutate(jsonb,uuid,text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_mutate(jsonb,uuid,text,jsonb) TO trellis_runtime_worker;

CREATE FUNCTION trellis_private.worker_read(p_claim jsonb,p_action text,p_payload jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='2s' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_result jsonb; v_checkpoint trellis.context_checkpoints%ROWTYPE;
    v_limit integer:=COALESCE((p_payload->>'limit')::integer,128); v_chat jsonb;
BEGIN
    v_run:=trellis_private.require_assignment(p_claim);
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR v_limit NOT BETWEEN 1 AND 501
       OR (p_payload ? 'run_id' AND p_payload->>'run_id' IS DISTINCT FROM v_run.id::text)
       OR (p_payload ? 'session_id' AND p_payload->>'session_id' IS DISTINCT FROM v_run.chat_id::text) THEN
        RAISE EXCEPTION 'read is outside execution assignment' USING ERRCODE='42501'; END IF;
    IF p_action IN ('context','credential') AND v_run.status<>'running' THEN
        RAISE EXCEPTION 'execution is not running' USING ERRCODE='42501'; END IF;
    SELECT to_jsonb(c)||jsonb_build_object('message_count',(SELECT count(*) FROM trellis.messages m
        WHERE m.user_id=v_run.user_id AND m.chat_id=c.id)) INTO v_chat
        FROM trellis.chats c WHERE c.id=v_run.chat_id AND c.user_id=v_run.user_id;
    IF p_action='credential' THEN
        IF p_payload->>'provider' IS DISTINCT FROM v_run.provider_id THEN
            RAISE EXCEPTION 'provider is outside execution assignment' USING ERRCODE='42501'; END IF;
        v_result:=to_jsonb(v_run.user_id);
    ELSIF p_action='context' THEN
        v_result:=jsonb_build_object('owner_id',v_run.user_id,'run',to_jsonb(v_run)-'lease_token',
            'session',v_chat,'model',(SELECT to_jsonb(m) FROM trellis.models m WHERE m.id=v_run.model_id));
    ELSIF p_action='runs' THEN v_result:=jsonb_build_array(to_jsonb(v_run)-'lease_token');
    ELSIF p_action='chats' THEN v_result:=jsonb_build_array(v_chat);
    ELSIF p_action='models' THEN
        SELECT jsonb_agg(to_jsonb(m)) INTO v_result FROM trellis.models m WHERE m.id=v_run.model_id;
    ELSIF p_action='messages' THEN
        SELECT jsonb_agg(to_jsonb(m) ORDER BY m.ordinal) INTO v_result FROM (
            SELECT * FROM trellis.messages WHERE user_id=v_run.user_id AND chat_id=v_run.chat_id
                AND (NOT p_payload ? 'id' OR id=(p_payload->>'id')::uuid)
                AND (NOT p_payload ? 'turn_id' OR turn_id=p_payload->>'turn_id')
                AND ordinal>COALESCE((p_payload->>'after_ordinal')::integer,0)
                ORDER BY ordinal LIMIT v_limit) m;
    ELSIF p_action='run_events' THEN
        SELECT jsonb_agg(to_jsonb(e) ORDER BY e.sequence) INTO v_result FROM (
            SELECT * FROM trellis.run_events WHERE user_id=v_run.user_id AND run_id=v_run.id
                AND sequence>COALESCE((p_payload->>'after_sequence')::integer,0)
                ORDER BY sequence LIMIT v_limit) e;
    ELSIF p_action='model_calls' THEN
        SELECT jsonb_agg(to_jsonb(c) ORDER BY step_index) INTO v_result FROM trellis.model_calls c
            WHERE user_id=v_run.user_id AND run_id=v_run.id;
    ELSIF p_action='run_messages' THEN
        SELECT jsonb_agg(to_jsonb(m) ORDER BY ordinal) INTO v_result FROM trellis.run_messages m
            WHERE user_id=v_run.user_id AND run_id=v_run.id;
    ELSIF p_action='tool_calls' THEN v_result:='[]'::jsonb;
    ELSIF p_action='run_assistant_outputs' THEN
        SELECT jsonb_agg(to_jsonb(o) ORDER BY step_index) INTO v_result FROM trellis.run_assistant_outputs o
            WHERE user_id=v_run.user_id AND run_id=v_run.id;
    ELSIF p_action IN ('context_checkpoints','context_window') THEN
        SELECT * INTO v_checkpoint FROM trellis.context_checkpoints WHERE user_id=v_run.user_id
            AND chat_id=v_run.chat_id ORDER BY revision DESC LIMIT 1;
        IF p_action='context_checkpoints' THEN
            v_result:=CASE WHEN v_checkpoint.id IS NULL THEN '[]'::jsonb ELSE jsonb_build_array(to_jsonb(v_checkpoint)) END;
        ELSE
            IF v_limit>128 THEN RAISE EXCEPTION 'context page limit is invalid' USING ERRCODE='22023'; END IF;
            SELECT jsonb_build_object('checkpoint',CASE WHEN v_checkpoint.id IS NULL THEN NULL ELSE to_jsonb(v_checkpoint) END,
                'messages',COALESCE(jsonb_agg(to_jsonb(m) ORDER BY m.ordinal),'[]'::jsonb)) INTO v_result
                FROM (SELECT * FROM trellis.messages WHERE user_id=v_run.user_id AND chat_id=v_run.chat_id
                    AND ordinal>COALESCE(v_checkpoint.covered_message_ordinal,0) ORDER BY ordinal LIMIT v_limit) m;
        END IF;
    ELSE RAISE EXCEPTION 'worker read is not permitted' USING ERRCODE='42501'; END IF;
    RETURN jsonb_build_object('owner_id',v_run.user_id,'result',COALESCE(v_result,'[]'::jsonb));
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_read(jsonb,text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_read(jsonb,text,jsonb) TO trellis_runtime_worker;

CREATE FUNCTION trellis_private.worker_renew(p_claim jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='2s' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_expiry timestamptz;
BEGIN
    v_run:=trellis_private.require_assignment(p_claim);
    v_expiry:=clock_timestamp()+interval '60 seconds';
    UPDATE trellis.runs SET lease_expires_at=v_expiry WHERE id=v_run.id RETURNING * INTO v_run;
    UPDATE trellis_private.jobs SET lease_expires_at=v_expiry,updated_at=clock_timestamp()
        WHERE id=(p_claim->>'job_id')::uuid;
    UPDATE trellis_private.execution_attempts SET lease_expires_at=v_expiry
        WHERE id=(p_claim->>'attempt_id')::uuid;
    RETURN to_jsonb(v_run)-'lease_token';
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_renew(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_renew(jsonb) TO trellis_runtime_worker;

CREATE OR REPLACE FUNCTION trellis_private.require_run(p_run_id uuid,p_capability text)
RETURNS trellis.runs LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_run trellis.runs%ROWTYPE;
BEGIN
    v_run:=trellis_private.require_run_owned(trellis_private.require_user(),p_run_id,p_capability);
    IF v_run.execution_mode='durable' THEN
        RAISE EXCEPTION 'worker authority required for durable execution' USING ERRCODE='42501'; END IF;
    RETURN v_run;
END;
$$;
COMMIT;
