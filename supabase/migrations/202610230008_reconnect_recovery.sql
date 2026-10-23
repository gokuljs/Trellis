BEGIN;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_reconnect_recovery;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_now timestamptz:=clock_timestamp(); v_result jsonb; v_event jsonb; v_limit integer;
    v_reason text; v_cancel boolean; v_usage_unknown boolean;
BEGIN
    IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' THEN
        RAISE EXCEPTION 'mutation payload must be an object' USING ERRCODE='22023';
    END IF;
    IF p_action='list_recovery_candidates' THEN
        v_limit:=COALESCE((p_payload->>'limit')::integer,20);
        IF v_limit NOT BETWEEN 1 AND 20 OR COALESCE(length(p_payload->>'installation_id'),0) NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid recovery discovery bounds' USING ERRCODE='22023';
        END IF;
        -- Include expired runs from other installations so policy rejection
        -- releases their session and they cannot starve later candidates.
        SELECT COALESCE(jsonb_agg(trellis_private.public_run(to_jsonb(r)) ORDER BY r.created_at,r.id),'[]'::jsonb)
        INTO v_result FROM (
            SELECT * FROM trellis.runs WHERE user_id=v_user AND recovery_contract IS NOT NULL
                AND status IN ('queued','recovering','running','waiting_for_approval','cancelling')
                AND (lease_expires_at IS NULL OR lease_expires_at<=v_now)
            ORDER BY created_at,id LIMIT v_limit
        ) r;
        RETURN v_result;
    ELSIF p_action='request_recovery_cancellation' THEN
        -- Stop is an authenticated user's intent, not permission to execute.
        -- It must survive reconnect before a replacement executor owns a lease.
        SELECT * INTO v_run FROM trellis.runs WHERE user_id=v_user AND id=(p_payload->>'run_id')::uuid FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
        IF v_run.recovery_contract IS NULL THEN
            RAISE EXCEPTION 'legacy run has no recovery contract' USING ERRCODE='22023';
        END IF;
        IF v_run.status IN ('completed','failed','cancelled','interrupted')
           OR v_run.cancel_requested_at IS NOT NULL THEN
            RETURN jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'event',NULL);
        END IF;
        UPDATE trellis.runs SET cancel_requested_at=v_now,status='cancelling',waiting_tool_call_id=NULL,
            recovery_reason='cancellation_requested'
        WHERE user_id=v_user AND id=v_run.id;
        BEGIN
            v_event:=trellis_private.append_event(v_user,v_run.id,'run.cancellation_requested',jsonb_build_object(
                'status','cancelling','generation',v_run.generation,'recovery_reason','cancellation_requested'));
        EXCEPTION WHEN invalid_parameter_value THEN
            IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
        END;
        SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id AND user_id=v_user;
        RETURN jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'event',v_event);
    ELSIF p_action='claim_recovery' THEN
        IF COALESCE(length(p_payload->>'installation_id'),0) NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid recovery installation identity' USING ERRCODE='22023';
        END IF;
        SELECT * INTO v_run FROM trellis.runs WHERE user_id=v_user AND id=(p_payload->>'run_id')::uuid FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
        IF v_run.recovery_contract IS NOT NULL
           AND v_run.recovery_contract->>'installation_id' IS DISTINCT FROM p_payload->>'installation_id' THEN
            IF v_run.status NOT IN ('queued','recovering','running','waiting_for_approval','cancelling') THEN
                v_reason:=CASE WHEN v_run.generation=(p_payload->>'expected_generation')::integer
                    AND v_run.recovery_reason IN ('different_installation','user_cancelled')
                    THEN v_run.recovery_reason ELSE 'terminal_run' END;
            ELSIF v_run.lease_expires_at>v_now THEN v_reason:='lease_active';
            ELSIF v_run.generation IS DISTINCT FROM (p_payload->>'expected_generation')::integer THEN
                v_reason:='generation_changed';
            ELSE
                -- No new capability, generation, budget, deadline, or attempt
                -- is allocated when the saved contract forbids this computer.
                v_cancel:=v_run.cancel_requested_at IS NOT NULL OR v_run.status='cancelling';
                v_reason:=CASE WHEN v_cancel THEN 'user_cancelled' ELSE 'different_installation' END;
                v_usage_unknown:=EXISTS(SELECT 1 FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id
                    AND (status='streaming' OR error_code='model_outcome_unknown'));
                UPDATE trellis.runs SET status=CASE WHEN v_cancel THEN 'cancelled' ELSE 'interrupted' END,
                    finished_at=v_now,lease_token=NULL,lease_expires_at=NULL,waiting_tool_call_id=NULL,
                    recovery_reason=v_reason,stop_reason=v_reason,error_code=v_reason,
                    error_message=CASE WHEN v_cancel THEN 'The run was cancelled.' ELSE
                        'This run belongs to another installation. Its unfinished work is preserved for review.' END
                WHERE user_id=v_user AND id=v_run.id RETURNING * INTO v_run;
                -- Terminal triggers preserve uncertain local effects and lost
                -- model usage before this event reads the resulting flags.
                BEGIN
                    v_event:=trellis_private.append_event(v_user,v_run.id,
                        CASE WHEN v_cancel THEN 'run.cancelled' ELSE 'run.interrupted' END,jsonb_build_object(
                            'status',v_run.status,'code',v_reason,'message',v_run.error_message,
                            'usage_unknown',v_usage_unknown));
                EXCEPTION WHEN invalid_parameter_value THEN
                    IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
                END;
                SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id AND user_id=v_user;
            END IF;
            RETURN jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),
                'lease_acquired',false,'rejection_reason',v_reason);
        END IF;
    END IF;
    RETURN trellis_private.mutate_before_reconnect_recovery(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_reconnect_recovery(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
