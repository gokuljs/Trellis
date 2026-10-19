BEGIN;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_run_stopping;
CREATE FUNCTION trellis_private.mutate(
    p_action text, p_payload jsonb, p_capability text DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_run trellis.runs%ROWTYPE;
    v_tool trellis.tool_calls%ROWTYPE;
    v_event jsonb;
    v_status text := p_payload->>'next_status';
    v_event_type text := p_payload->>'event_type';
BEGIN
    IF p_action = 'resume_approved_run' THEN
        v_run := trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        IF v_run.status <> 'waiting_for_approval' OR v_run.waiting_tool_call_id IS NULL THEN
            RAISE EXCEPTION 'run not waiting for approval' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_tool FROM trellis.tool_calls
        WHERE id = v_run.waiting_tool_call_id AND user_id = v_user AND run_id = v_run.id
          AND status = 'pending' AND approval_decision IS NOT NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'waiting tool call has no approval decision' USING ERRCODE = '22023';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM trellis.run_events
            WHERE user_id = v_user AND run_id = v_run.id AND event_type = 'tool.approval_requested'
              AND data->>'tool_call_id' = v_tool.id::text) THEN
            RAISE EXCEPTION 'waiting tool call has no approval request' USING ERRCODE = '22023';
        END IF;
        UPDATE trellis.runs SET status = 'running', waiting_tool_call_id = NULL
        WHERE id = v_run.id AND user_id = v_user RETURNING * INTO v_run;
        v_event := trellis_private.append_event(v_user,v_run.id,'run.resumed',
            pg_catalog.jsonb_build_object('tool_call_id',v_tool.id,'deadline_at',v_run.deadline_at));
        RETURN pg_catalog.jsonb_build_object(
            'run',pg_catalog.to_jsonb(v_run) - 'lease_token' - 'request_fingerprint','event',v_event);
    END IF;
    IF p_action = 'transition_run_record' AND v_status IN ('failed','interrupted') THEN
        v_run := trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        IF v_run.status = 'waiting_for_approval' THEN
            IF v_event_type IS DISTINCT FROM 'run.' || v_status THEN
                RAISE EXCEPTION 'invalid run transition or lifecycle event' USING ERRCODE = '22023';
            END IF;
            UPDATE trellis.runs SET status = v_status, finished_at = pg_catalog.now(),
                error_code = p_payload->>'error_code', error_message = p_payload->>'error_message',
                stop_reason = p_payload->>'stop_reason', waiting_tool_call_id = NULL,
                lease_token = NULL, lease_expires_at = NULL
            WHERE id = v_run.id AND user_id = v_user;
            RETURN trellis_private.append_event(v_user,v_run.id,v_event_type,
                COALESCE(p_payload->'data','{}'::jsonb));
        END IF;
    END IF;
    RETURN trellis_private.mutate_before_run_stopping(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_run_stopping(text,jsonb,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
