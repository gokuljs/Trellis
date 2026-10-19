BEGIN;

ALTER TABLE trellis.runs ADD COLUMN request_fingerprint text
    CHECK (request_fingerprint ~ '^[0-9a-f]{64}$');

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_reliable_runs;
CREATE FUNCTION trellis_private.mutate(
    p_action text, p_payload jsonb, p_capability text DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_run trellis.runs%ROWTYPE;
    v_result jsonb;
    v_fingerprint text := p_payload->>'request_fingerprint';
    v_owned boolean;
BEGIN
    IF p_action NOT IN ('lookup_run','match_run_request','create_run') THEN
        RETURN trellis_private.mutate_before_reliable_runs(p_action,p_payload,p_capability);
    END IF;
    IF p_payload IS NULL OR pg_catalog.jsonb_typeof(p_payload) <> 'object'
       OR COALESCE(pg_catalog.length(p_payload->>'client_request_id'),0) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'invalid run request' USING ERRCODE = '22023';
    END IF;
    IF p_action = 'lookup_run' THEN
        SELECT * INTO v_run FROM trellis.runs
        WHERE user_id = v_user AND chat_id = (p_payload->>'session_id')::uuid
          AND client_request_id = p_payload->>'client_request_id';
        IF NOT FOUND THEN RETURN NULL; END IF;
        RETURN pg_catalog.to_jsonb(v_run) - 'lease_token' - 'request_fingerprint';
    END IF;
    IF v_fingerprint IS NOT NULL AND v_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid request fingerprint' USING ERRCODE = '22023';
    END IF;
    IF p_action = 'create_run' THEN
        PERFORM 1 FROM trellis.chats
        WHERE user_id = v_user AND id = (p_payload->>'session_id')::uuid FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'session not found' USING ERRCODE = '22023'; END IF;
    END IF;
    SELECT * INTO v_run FROM trellis.runs
    WHERE user_id = v_user AND chat_id = (p_payload->>'session_id')::uuid
      AND client_request_id = p_payload->>'client_request_id';
    IF FOUND THEN
        IF v_run.request_fingerprint IS NOT NULL THEN
            IF v_run.request_fingerprint IS DISTINCT FROM v_fingerprint THEN
                RAISE EXCEPTION 'run request conflicts with a previous payload' USING ERRCODE = '22023';
            END IF;
        ELSIF v_run.turn_id IS DISTINCT FROM p_payload->>'turn_id'
          OR NOT EXISTS (SELECT 1 FROM trellis.messages WHERE user_id = v_user
              AND id = v_run.input_message_id AND content = p_payload->>'content')
          OR (p_payload->>'submitted_model_id' IS NOT NULL
              AND v_run.model_id IS DISTINCT FROM p_payload->>'submitted_model_id')
          OR (p_payload->>'submitted_budget_preset' IS NOT NULL
              AND v_run.budget_preset IS DISTINCT FROM p_payload->>'submitted_budget_preset') THEN
            RAISE EXCEPTION 'run request conflicts with a previous payload' USING ERRCODE = '22023';
        END IF;
        v_owned := p_capability IS NOT NULL
            AND v_run.lease_token = trellis_private.capability_digest(p_capability)
            AND v_run.lease_expires_at > pg_catalog.clock_timestamp()
            AND v_run.status IN ('queued','running','waiting_for_approval','cancelling');
        RETURN pg_catalog.jsonb_build_object(
            'run', pg_catalog.to_jsonb(v_run) - 'lease_token' - 'request_fingerprint',
            'lease_acquired', COALESCE(v_owned,false));
    END IF;
    IF p_action = 'match_run_request' THEN RETURN NULL; END IF;

    SELECT * INTO v_run FROM trellis.runs
    WHERE user_id = v_user AND chat_id = (p_payload->>'session_id')::uuid
      AND turn_id = p_payload->>'turn_id'
    ORDER BY created_at DESC,id DESC LIMIT 1;
    IF FOUND AND v_run.status IN ('queued','running','waiting_for_approval','cancelling','completed')
       AND v_run.request_fingerprint IS NOT NULL
       AND v_run.request_fingerprint IS DISTINCT FROM v_fingerprint THEN
        RAISE EXCEPTION 'run request conflicts with a previous payload' USING ERRCODE = '22023';
    END IF;
    v_result := trellis_private.mutate_before_reliable_runs('create_run',p_payload,p_capability);
    IF (v_result->>'lease_acquired')::boolean THEN
        UPDATE trellis.runs SET request_fingerprint = v_fingerprint
        WHERE id = (v_result#>>'{run,id}')::uuid AND user_id = v_user RETURNING * INTO v_run;
        v_result := pg_catalog.jsonb_set(v_result,'{run}',pg_catalog.to_jsonb(v_run) - 'lease_token');
    END IF;
    RETURN pg_catalog.jsonb_set(v_result,'{run}',(v_result->'run') - 'request_fingerprint');
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_reliable_runs(text,jsonb,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
