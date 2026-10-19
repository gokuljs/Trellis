BEGIN;

ALTER TABLE trellis.runs
    ADD COLUMN execution_mode text NOT NULL DEFAULT 'legacy'
        CHECK (execution_mode IN ('legacy', 'durable')),
    ADD COLUMN request_fingerprint text,
    ADD COLUMN max_seconds integer NOT NULL DEFAULT 600 CHECK (max_seconds IN (600, 1200)),
    ADD COLUMN worker_id text,
    ADD COLUMN attempt_id uuid,
    ALTER COLUMN deadline_at DROP NOT NULL,
    ADD CONSTRAINT legacy_runs_have_deadline CHECK (
        execution_mode = 'durable' OR deadline_at IS NOT NULL
    ),
    ADD CONSTRAINT durable_runs_have_submission CHECK (
        execution_mode = 'legacy' OR
            (request_fingerprint IS NOT NULL AND request_fingerprint ~ '^[a-f0-9]{64}$')
    );
UPDATE trellis.runs SET max_seconds = 1200 WHERE budget_preset = 'longer';

CREATE TABLE trellis_private.jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL UNIQUE,
    user_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'claimed', 'running', 'finished')),
    worker_id text,
    attempt_id uuid,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, run_id, id),
    FOREIGN KEY (user_id, run_id) REFERENCES trellis.runs(user_id, id) ON DELETE CASCADE,
    CHECK (status <> 'ready' OR
        (worker_id IS NULL AND attempt_id IS NULL AND lease_expires_at IS NULL)),
    CHECK (status NOT IN ('claimed', 'running') OR
        (worker_id IS NOT NULL AND attempt_id IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE INDEX jobs_ready_order ON trellis_private.jobs(created_at, id) WHERE status = 'ready';
CREATE INDEX jobs_expiry ON trellis_private.jobs(lease_expires_at, id)
    WHERE status IN ('claimed', 'running');

CREATE TABLE trellis_private.execution_attempts (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    user_id uuid NOT NULL,
    worker_id text NOT NULL CHECK (length(worker_id) BETWEEN 1 AND 200),
    status text NOT NULL CHECK (status IN ('claimed', 'running', 'finished')),
    capability_digest text NOT NULL CHECK (capability_digest ~ '^[a-f0-9]{64}$'),
    lease_expires_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    outcome text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, run_id, id),
    FOREIGN KEY (user_id, run_id, job_id)
        REFERENCES trellis_private.jobs(user_id, run_id, id) ON DELETE CASCADE
);
ALTER TABLE trellis_private.jobs ADD CONSTRAINT jobs_current_attempt_fk
    FOREIGN KEY (user_id, run_id, attempt_id)
        REFERENCES trellis_private.execution_attempts(user_id, run_id, id)
        DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE trellis.runs ADD CONSTRAINT runs_current_attempt_fk
    FOREIGN KEY (user_id, id, attempt_id)
        REFERENCES trellis_private.execution_attempts(user_id, run_id, id)
        DEFERRABLE INITIALLY DEFERRED;
REVOKE ALL ON trellis_private.jobs, trellis_private.execution_attempts
    FROM PUBLIC, anon, authenticated;

CREATE FUNCTION trellis_private.trim_submission_content(p_content text)
RETURNS text LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path = '' AS $$
    SELECT pg_catalog.btrim(p_content,
        U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680' ||
        U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A' ||
        U&'\2028\2029\202F\205F\3000');
$$;
REVOKE ALL ON FUNCTION trellis_private.trim_submission_content(text)
    FROM PUBLIC, anon, authenticated;

CREATE FUNCTION trellis_private.submission_fingerprint(p_submission jsonb, p_request_id text)
RETURNS text LANGUAGE plpgsql IMMUTABLE SET search_path = '' AS $$
DECLARE v_input jsonb; v_content text; v_turn text;
BEGIN
    IF jsonb_typeof(p_submission) IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_submission->'content') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'invalid run submission' USING ERRCODE = '22023';
    END IF;
    v_content := trellis_private.trim_submission_content(p_submission->>'content');
    v_turn := COALESCE(p_submission->>'turnId', p_request_id);
    IF length(v_content) NOT BETWEEN 1 AND 100000 OR length(v_turn) NOT BETWEEN 1 AND 200
       OR (p_submission ? 'turnId' AND jsonb_typeof(p_submission->'turnId') <> 'string')
       OR EXISTS (SELECT 1 FROM jsonb_object_keys(p_submission) k
           WHERE k NOT IN ('content', 'turnId', 'modelId', 'budgetPreset')) THEN
        RAISE EXCEPTION 'invalid run submission' USING ERRCODE = '22023';
    END IF;
    v_input := jsonb_build_object('content', v_content, 'turnId', v_turn);
    IF p_submission ? 'modelId' THEN
        IF jsonb_typeof(p_submission->'modelId') IS DISTINCT FROM 'string'
           OR length(p_submission->>'modelId') NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'invalid run submission' USING ERRCODE = '22023';
        END IF;
        v_input := v_input || jsonb_build_object('modelId', p_submission->>'modelId');
    END IF;
    IF p_submission ? 'budgetPreset' THEN
        IF p_submission->>'budgetPreset' NOT IN ('conservative', 'longer')
           OR jsonb_typeof(p_submission->'budgetPreset') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'invalid run submission' USING ERRCODE = '22023';
        END IF;
        v_input := v_input || jsonb_build_object('budgetPreset', p_submission->>'budgetPreset');
    END IF;
    RETURN encode(sha256(convert_to(v_input::text, 'UTF8')), 'hex');
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.submission_fingerprint(jsonb, text)
    FROM PUBLIC, anon, authenticated;

CREATE FUNCTION trellis.lookup_run(p_session_id uuid, p_client_request_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_user uuid := trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM trellis.chats WHERE user_id = v_user AND id = p_session_id) THEN
        RAISE EXCEPTION 'session not found' USING ERRCODE = '22023';
    END IF;
    IF p_client_request_id IS NULL OR length(p_client_request_id) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'invalid client request ID' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE user_id = v_user AND chat_id = p_session_id
        AND client_request_id = p_client_request_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN to_jsonb(v_run) - 'lease_token' - 'request_fingerprint';
END;
$$;

CREATE FUNCTION trellis.match_run_request(p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_run jsonb; v_fingerprint text;
BEGIN
    v_run := trellis.lookup_run((p_payload->>'session_id')::uuid, p_payload->>'client_request_id');
    v_fingerprint := trellis_private.submission_fingerprint(
        p_payload->'submission', p_payload->>'client_request_id');
    IF v_run IS NULL THEN RETURN NULL; END IF;
    IF NOT EXISTS (SELECT 1 FROM trellis.runs WHERE id = (v_run->>'id')::uuid
        AND user_id = auth.uid() AND request_fingerprint = v_fingerprint) THEN
        RAISE EXCEPTION 'run request conflicts with a previous payload' USING ERRCODE = '22023';
    END IF;
    RETURN v_run;
END;
$$;

CREATE FUNCTION trellis.accept_run(p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' SET lock_timeout = '3s' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_chat trellis.chats%ROWTYPE; v_model trellis.models%ROWTYPE;
    v_run trellis.runs%ROWTYPE; v_prior trellis.runs%ROWTYPE; v_message trellis.messages%ROWTYPE;
    v_existing jsonb; v_fingerprint text; v_content text; v_turn text; v_budget text;
    v_ordinal integer; v_calls integer; v_tools integer; v_tokens integer;
    v_cost numeric; v_seconds integer;
BEGIN
    -- One account lock serializes backlog admission across different chats.
    -- Completion's message foreign key must still be able to take KEY SHARE.
    PERFORM 1 FROM trellis.profiles WHERE id = v_user FOR NO KEY UPDATE;
    SELECT * INTO v_chat FROM trellis.chats
        WHERE id = (p_payload->>'session_id')::uuid AND user_id = v_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'session not found' USING ERRCODE = '22023'; END IF;
    v_existing := trellis.match_run_request(p_payload);
    IF v_existing IS NOT NULL THEN RETURN v_existing; END IF;
    v_fingerprint := trellis_private.submission_fingerprint(
        p_payload->'submission', p_payload->>'client_request_id');
    v_content := trellis_private.trim_submission_content(p_payload->>'content');
    v_turn := COALESCE(p_payload->'submission'->>'turnId', p_payload->>'client_request_id');
    v_budget := p_payload->>'budget_preset';
    IF v_content IS NULL OR length(v_content) NOT BETWEEN 1 AND 100000 THEN
        RAISE EXCEPTION 'run input cannot be empty or exceed 100000 characters' USING ERRCODE = '22023';
    END IF;
    IF (p_payload->'submission' ? 'modelId' AND
        p_payload->'submission'->>'modelId' IS DISTINCT FROM p_payload->>'model_id')
       OR (p_payload->'submission' ? 'budgetPreset' AND
        p_payload->'submission'->>'budgetPreset' IS DISTINCT FROM v_budget) THEN
        RAISE EXCEPTION 'resolved input conflicts with submission' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_model FROM trellis.models
        WHERE id = p_payload->>'model_id' AND enabled AND metadata_status = 'ready';
    IF NOT FOUND THEN RAISE EXCEPTION 'model not found' USING ERRCODE = '22023'; END IF;
    IF v_budget = 'conservative' THEN
        v_calls := 8; v_tools := 16; v_tokens := 100000; v_cost := 2; v_seconds := 600;
    ELSIF v_budget = 'longer' THEN
        v_calls := 15; v_tools := 30; v_tokens := 200000; v_cost := 5; v_seconds := 1200;
    ELSE RAISE EXCEPTION 'unknown run budget' USING ERRCODE = '22023'; END IF;
    SELECT * INTO v_prior FROM trellis.runs WHERE user_id = v_user AND chat_id = v_chat.id
        AND turn_id = v_turn ORDER BY created_at DESC, id DESC LIMIT 1;
    IF FOUND THEN
        SELECT * INTO v_message FROM trellis.messages WHERE user_id = v_user
            AND id = v_prior.input_message_id;
        IF v_message.content <> v_content THEN
            RAISE EXCEPTION 'run request conflicts with a previous payload' USING ERRCODE = '22023';
        END IF;
        IF v_prior.status IN ('queued', 'running', 'waiting_for_approval', 'cancelling', 'completed') THEN
            RETURN to_jsonb(v_prior) - 'lease_token' - 'request_fingerprint';
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM trellis.turn_claims
        WHERE user_id = v_user AND chat_id = v_chat.id
            AND claimed_at > pg_catalog.clock_timestamp() - interval '5 minutes') THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'session_turn_busy';
    END IF;
    IF EXISTS (SELECT 1 FROM trellis.runs WHERE user_id = v_user AND chat_id = v_chat.id
        AND status IN ('queued', 'running', 'waiting_for_approval', 'cancelling')) THEN
        RAISE EXCEPTION 'active run already exists for this session' USING ERRCODE = '22023';
    END IF;
    IF (SELECT count(*) FROM trellis.runs WHERE user_id = v_user
        AND status IN ('queued', 'running', 'waiting_for_approval', 'cancelling')) >= 16 THEN
        RAISE EXCEPTION 'account run capacity reached' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_message FROM trellis.messages WHERE user_id = v_user AND chat_id = v_chat.id
        AND turn_id = v_turn AND role = 'user';
    IF FOUND THEN
        IF v_message.content <> v_content THEN
            RAISE EXCEPTION 'run request conflicts with a previous payload' USING ERRCODE = '22023';
        END IF;
    ELSE
        SELECT COALESCE(max(ordinal), 0) + 1 INTO v_ordinal FROM trellis.messages
            WHERE user_id = v_user AND chat_id = v_chat.id;
        INSERT INTO trellis.messages(user_id, chat_id, turn_id, ordinal, role, content)
            VALUES(v_user, v_chat.id, v_turn, v_ordinal, 'user', v_content)
            RETURNING * INTO v_message;
        IF v_ordinal = 1 THEN
            UPDATE trellis.chats SET title = left(regexp_replace(v_content, '\s+', ' ', 'g'), 80)
                WHERE id = v_chat.id AND user_id = v_user;
        END IF;
    END IF;
    INSERT INTO trellis.runs(user_id, chat_id, turn_id, input_message_id, client_request_id,
        retry_of, status, provider_id, model_id, adapter_kind, upstream_model_id, budget_preset,
        max_model_calls, max_tool_calls, max_total_tokens, max_cost_usd, deadline_at,
        execution_mode, request_fingerprint, max_seconds)
    VALUES(v_user, v_chat.id, v_turn, v_message.id, p_payload->>'client_request_id',
        v_prior.id, 'queued', v_model.provider_id, v_model.id, v_model.adapter_kind,
        v_model.upstream_model_id, v_budget, v_calls, v_tools, v_tokens, v_cost, NULL,
        'durable', v_fingerprint, v_seconds) RETURNING * INTO v_run;
    UPDATE trellis.messages SET run_id = v_run.id WHERE id = v_message.id AND user_id = v_user;
    INSERT INTO trellis_private.jobs(user_id, run_id) VALUES(v_user, v_run.id);
    PERFORM trellis_private.append_event(v_user, v_run.id, 'run.queued',
        jsonb_build_object('session_id', v_chat.id, 'turn_id', v_turn, 'status', 'queued'));
    SELECT * INTO v_run FROM trellis.runs WHERE id = v_run.id AND user_id = v_user;
    RETURN to_jsonb(v_run) - 'lease_token' - 'request_fingerprint';
END;
$$;
REVOKE ALL ON FUNCTION trellis.lookup_run(uuid, text), trellis.match_run_request(jsonb),
    trellis.accept_run(jsonb) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION trellis.lookup_run(uuid, text), trellis.match_run_request(jsonb),
    trellis.accept_run(jsonb) TO authenticated;
COMMIT;
