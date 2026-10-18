-- Execution helpers take an owner explicitly. Only a validated worker assignment
-- or the authenticated browser boundary may select that owner.
BEGIN;
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='trellis_runtime_worker') THEN
        CREATE ROLE trellis_runtime_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 16;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='trellis_runtime_worker'
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolinherit OR rolreplication OR rolbypassrls OR NOT rolcanlogin))
       OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='trellis_runtime_worker')) THEN
        RAISE EXCEPTION 'worker login must have restricted attributes and no role memberships';
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
    v_attempt trellis_private.execution_attempts%ROWTYPE; v_now timestamptz;
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
    v_now:=clock_timestamp();
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
        PERFORM trellis_private.append_event(v_run.user_id,v_run.id,'run.started',
            jsonb_build_object('status','running','deadline_at',v_now+make_interval(secs=>v_run.max_seconds)));
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id;
    RETURN to_jsonb(v_run)-'lease_token';
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_begin(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_begin(jsonb) TO trellis_runtime_worker;
ALTER TABLE trellis_private.execution_attempts
    ADD COLUMN operation_storage_count integer NOT NULL DEFAULT 0 CHECK(operation_storage_count BETWEEN 0 AND 32784),
    ADD COLUMN operation_storage_bytes bigint NOT NULL DEFAULT 0 CHECK(operation_storage_bytes BETWEEN 0 AND 16777216);
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
    v_fingerprint text; v_call_id uuid; v_receipt jsonb; v_content text; v_length integer; v_request jsonb; v_terminal boolean; v_size bigint;
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
        v_receipt:=v_prior.result;
        IF v_receipt ? '_model_request_call_id' THEN
            SELECT request_snapshot INTO v_request FROM trellis.model_calls
                WHERE id=(v_receipt->>'_model_request_call_id')::uuid AND run_id=v_run.id AND user_id=v_run.user_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'publication receipt is unavailable' USING ERRCODE='22023'; END IF;
            v_receipt:=jsonb_set(v_receipt-'_model_request_call_id','{request_snapshot}',v_request);
        END IF;
        IF v_receipt ? '_output_content_length' THEN
            v_length:=(v_receipt->>'_output_content_length')::integer;
            SELECT content INTO v_content FROM trellis.run_assistant_outputs
                WHERE id=COALESCE(v_receipt->'output'->>'id',v_receipt->>'id')::uuid
                    AND run_id=v_run.id AND user_id=v_run.user_id;
            IF v_content IS NULL OR char_length(v_content)<v_length THEN
                RAISE EXCEPTION 'publication receipt is unavailable' USING ERRCODE='22023'; END IF;
            v_receipt:=jsonb_set(v_receipt-'_output_content_length',
                CASE WHEN v_receipt ? 'output' THEN '{output,content}'::text[] ELSE '{content}'::text[] END,
                to_jsonb(left(v_content,v_length)));
        END IF;
        RETURN v_receipt;
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
    v_result:=trellis_private.mutate_owned(v_run.user_id,p_action,
        p_payload||jsonb_build_object('run_id',v_run.id),p_claim->>'capability');
    SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id;
    IF v_run.status IN ('completed','failed','cancelled','interrupted') THEN
        UPDATE trellis_private.jobs SET status='finished',updated_at=clock_timestamp()
            WHERE id=(p_claim->>'job_id')::uuid;
        UPDATE trellis_private.execution_attempts SET status='finished',finished_at=clock_timestamp(),outcome=v_run.status
            WHERE id=(p_claim->>'attempt_id')::uuid;
    END IF;
    v_receipt:=COALESCE(v_result,'null'::jsonb);
    IF p_action='append_assistant_batch' THEN
        -- The output is an append-only prefix. Saving it for every batch would
        -- turn a bounded transcript into a quadratic retry journal.
        v_receipt:=jsonb_set(v_result,'{output}',(v_result->'output')-'content')
            ||jsonb_build_object('_output_content_length',char_length(v_result->'output'->>'content'));
    END IF;
    IF p_action='finish_assistant_output' AND v_result IS NOT NULL AND v_result<>'null'::jsonb THEN
        v_receipt:=(v_result-'content')||jsonb_build_object('_output_content_length',char_length(v_result->>'content'));
    END IF;
    IF p_action IN ('create_model_call','update_model_call') THEN
        v_receipt:=(v_result-'request_snapshot')||jsonb_build_object('_model_request_call_id',v_result->>'id');
    END IF;
    v_terminal:=p_action='complete_run' OR p_action='transition_run_record';
    v_size:=octet_length(v_receipt::text);
    UPDATE trellis_private.execution_attempts SET operation_storage_count=operation_storage_count+1,
        operation_storage_bytes=operation_storage_bytes+v_size
        WHERE id=(p_claim->>'attempt_id')::uuid
            AND operation_storage_count+1<=CASE WHEN v_terminal THEN 32784 ELSE 32768 END
            AND operation_storage_bytes+v_size<=CASE WHEN v_terminal THEN 16777216 ELSE 8388608 END;
    IF NOT FOUND THEN RAISE EXCEPTION 'worker_operation_storage_limit' USING ERRCODE='22023'; END IF;
    INSERT INTO trellis_private.worker_operations(attempt_id,operation_id,fingerprint,result)
        VALUES((p_claim->>'attempt_id')::uuid,p_operation_id,v_fingerprint,v_receipt);
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_mutate(jsonb,uuid,text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_mutate(jsonb,uuid,text,jsonb) TO trellis_runtime_worker;

CREATE FUNCTION trellis_private.worker_read(p_claim jsonb,p_action text,p_payload jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='2s' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_result jsonb; v_checkpoint trellis.context_checkpoints%ROWTYPE;
    v_limit integer:=COALESCE((p_payload->>'limit')::integer,128);
    v_offset integer:=COALESCE((p_payload->>'offset')::integer,0); v_chat jsonb;
BEGIN
    v_run:=trellis_private.require_assignment(p_claim);
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR v_limit NOT BETWEEN 1 AND 501 OR v_offset NOT BETWEEN 0 AND 1000000
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
                ORDER BY ordinal LIMIT v_limit OFFSET v_offset) m;
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
    IF v_offset>0 AND p_action<>'messages' THEN v_result:='[]'::jsonb; END IF;
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
    IF v_run.status<>'queued' AND v_run.deadline_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'execution budget expired' USING ERRCODE='42501'; END IF;
    v_expiry:=LEAST(clock_timestamp()+interval '60 seconds',v_run.deadline_at);
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
CREATE FUNCTION trellis_private.release_assignment(p_run trellis.runs,p_outcome text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    UPDATE trellis_private.execution_attempts SET status='finished',finished_at=clock_timestamp(),outcome=p_outcome
        WHERE id=p_run.attempt_id;
    UPDATE trellis_private.jobs SET status='ready',worker_id=NULL,attempt_id=NULL,
        lease_expires_at=NULL,updated_at=clock_timestamp() WHERE run_id=p_run.id;
    UPDATE trellis.runs SET worker_id=NULL,attempt_id=NULL,lease_token=NULL,lease_expires_at=NULL,
        recovery_count=recovery_count+CASE WHEN p_outcome='expired' THEN 1 ELSE 0 END WHERE id=p_run.id;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.release_assignment(trellis.runs,text)
    FROM PUBLIC,anon,authenticated,trellis_runtime_worker;

CREATE FUNCTION trellis_private.worker_release_unstarted(p_claim jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='2s' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_job trellis_private.jobs%ROWTYPE;
    v_attempt trellis_private.execution_attempts%ROWTYPE;
BEGIN
    PERFORM trellis_private.require_worker();
    SELECT * INTO v_run FROM trellis.runs WHERE id=(p_claim->>'run_id')::uuid FOR UPDATE;
    SELECT * INTO v_job FROM trellis_private.jobs WHERE id=(p_claim->>'job_id')::uuid AND run_id=v_run.id FOR UPDATE;
    SELECT * INTO v_attempt FROM trellis_private.execution_attempts WHERE id=(p_claim->>'attempt_id')::uuid
        AND job_id=v_job.id AND run_id=v_run.id;
    IF v_attempt.status='finished' AND v_attempt.outcome='released' AND v_job.status='ready'
       AND v_run.attempt_id IS NULL AND v_run.status='queued'
       AND v_attempt.worker_id=p_claim->>'worker_id' AND v_attempt.lease_expires_at>clock_timestamp()
       AND v_attempt.capability_digest=trellis_private.capability_digest(p_claim->>'capability') THEN
        RETURN;
    END IF;
    v_run:=trellis_private.require_assignment(p_claim);
    IF v_run.status<>'queued' THEN
        RAISE EXCEPTION 'started execution cannot be released' USING ERRCODE='42501'; END IF;
    PERFORM trellis_private.release_assignment(v_run,'released');
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_release_unstarted(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_release_unstarted(jsonb) TO trellis_runtime_worker;

CREATE FUNCTION trellis_private.worker_recover_expired(p_limit integer DEFAULT 100)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='2s' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_job trellis_private.jobs%ROWTYPE; v_count integer:=0; v_status text;
BEGIN
    PERFORM trellis_private.require_worker();
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'expiry recovery limit is invalid' USING ERRCODE='22023'; END IF;
    FOR v_run IN SELECT r.* FROM trellis.runs r JOIN trellis_private.jobs j ON j.run_id=r.id
        WHERE j.status IN ('claimed','running') AND j.lease_expires_at<=clock_timestamp()
        ORDER BY j.lease_expires_at,j.id LIMIT p_limit FOR UPDATE OF r SKIP LOCKED
    LOOP
        SELECT * INTO v_job FROM trellis_private.jobs WHERE run_id=v_run.id FOR UPDATE;
        IF v_job.status NOT IN ('claimed','running') OR v_job.lease_expires_at>clock_timestamp() THEN CONTINUE; END IF;
        IF v_job.status='claimed' AND v_run.status='queued' AND v_run.started_at IS NULL THEN
            PERFORM trellis_private.release_assignment(v_run,'expired');
        ELSE
            v_status:=CASE WHEN v_run.status='cancelling' THEN 'cancelled' ELSE 'interrupted' END;
            UPDATE trellis.runs SET status=v_status,finished_at=clock_timestamp(),
                stop_reason=CASE WHEN v_status='cancelled' THEN 'user_cancelled' ELSE 'lease_expired' END,
                error_code=CASE WHEN v_status='cancelled' THEN NULL ELSE 'lease_expired' END,
                error_message='The worker stopped before this request finished.',
                waiting_tool_call_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                recovery_count=recovery_count+1 WHERE id=v_run.id;
            UPDATE trellis_private.jobs SET status='finished',updated_at=clock_timestamp() WHERE id=v_job.id;
            UPDATE trellis_private.execution_attempts SET status='finished',finished_at=clock_timestamp(),outcome=v_status
                WHERE id=v_job.attempt_id;
            PERFORM trellis_private.append_event(v_run.user_id,v_run.id,'run.'||v_status,
                jsonb_build_object('status',v_status,'code',CASE WHEN v_status='cancelled' THEN 'user_cancelled' ELSE 'lease_expired' END,
                    'message','The worker stopped before this request finished.'));
        END IF;
        v_count:=v_count+1;
    END LOOP;
    RETURN v_count;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.worker_recover_expired(integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.worker_recover_expired(integer) TO trellis_runtime_worker;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_worker_authority;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_worker_authority(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_runtime_worker;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_owner uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_run_id uuid; v_event jsonb; v_now timestamptz;
BEGIN
    v_run_id:=(p_payload->>'run_id')::uuid;
    IF v_run_id IS NULL AND p_payload ? 'call_id' THEN
        SELECT run_id INTO v_run_id FROM trellis.model_calls WHERE id=(p_payload->>'call_id')::uuid AND user_id=v_owner;
    END IF;
    IF v_run_id IS NOT NULL THEN
        SELECT * INTO v_run FROM trellis.runs WHERE id=v_run_id AND user_id=v_owner FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
        IF v_run.execution_mode='durable' THEN
            IF p_action='request_run_cancellation' THEN
                IF v_run.status IN ('cancelling','completed','failed','cancelled','interrupted') THEN
                    RETURN jsonb_build_object('run',to_jsonb(v_run)-'lease_token','event',NULL); END IF;
                v_now:=clock_timestamp();
                UPDATE trellis.runs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,
                    cancel_requested_at=COALESCE(cancel_requested_at,v_now),
                    finished_at=CASE WHEN status='queued' THEN v_now ELSE NULL END,
                    stop_reason=CASE WHEN status='queued' THEN 'user_cancelled' ELSE stop_reason END,
                    lease_token=CASE WHEN status='queued' THEN NULL ELSE lease_token END,
                    lease_expires_at=CASE WHEN status='queued' THEN NULL ELSE lease_expires_at END
                    WHERE id=v_run.id RETURNING * INTO v_run;
                IF v_run.status='cancelled' THEN
                    UPDATE trellis_private.jobs SET status='finished',updated_at=v_now WHERE run_id=v_run.id;
                    UPDATE trellis_private.execution_attempts SET status='finished',finished_at=v_now,outcome='cancelled'
                        WHERE id=v_run.attempt_id;
                END IF;
                v_event:=trellis_private.append_event(v_owner,v_run.id,
                    CASE WHEN v_run.status='cancelled' THEN 'run.cancelled' ELSE 'run.cancellation_requested' END,
                    jsonb_build_object('status',v_run.status,'code','user_cancelled','message','Cancellation requested.'));
                SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id;
                RETURN jsonb_build_object('run',to_jsonb(v_run)-'lease_token','event',v_event);
            ELSIF p_action='record_tool_approval_decision' THEN
                RAISE EXCEPTION 'durable chat has no tool approvals' USING ERRCODE='22023';
            ELSE
                RAISE EXCEPTION 'worker authority required for durable execution' USING ERRCODE='42501';
            END IF;
        END IF;
    END IF;
    RETURN trellis_private.mutate_before_worker_authority(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated,trellis_runtime_worker;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
COMMIT;
