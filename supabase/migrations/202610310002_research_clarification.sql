BEGIN;

ALTER TABLE trellis.runs DROP CONSTRAINT runs_status_check;
ALTER TABLE trellis.runs ADD CONSTRAINT runs_status_check CHECK (status IN (
    'queued','recovering','running','waiting_for_approval','waiting_for_input','cancelling',
    'completed','failed','cancelled','interrupted'));
DROP INDEX trellis.runs_one_active_per_chat;
CREATE UNIQUE INDEX runs_one_active_per_chat ON trellis.runs(user_id,chat_id)
    WHERE status IN ('queued','recovering','running','waiting_for_approval','waiting_for_input','cancelling');

ALTER FUNCTION trellis_private.research_mutate(text,jsonb,text) RENAME TO research_mutate_before_clarification;
CREATE FUNCTION trellis_private.research_mutate(p_action text,p_payload jsonb,p_capability text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_state trellis.research_runs%ROWTYPE; v_op trellis.research_operations%ROWTYPE;
    v_now timestamptz:=clock_timestamp(); v_question uuid; v_question_text text;
    v_op_id uuid; v_fingerprint text; v_event jsonb; v_result jsonb; v_text text;
BEGIN
    IF p_action NOT IN ('pause','answer') THEN
        RETURN trellis_private.research_mutate_before_clarification(p_action,p_payload,p_capability);
    END IF;
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR octet_length(p_payload::text)>40000 THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE user_id=v_user AND id=(p_payload->>'run_id')::uuid FOR UPDATE;
    IF NOT FOUND OR v_run.run_mode<>'research' THEN RAISE EXCEPTION 'research_not_found' USING ERRCODE='42501'; END IF;
    SELECT * INTO v_state FROM trellis.research_runs WHERE user_id=v_user AND run_id=v_run.id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'research_not_found' USING ERRCODE='22023'; END IF;
    v_question:=(p_payload->>'clarification_id')::uuid;
    IF v_question IS NULL THEN RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023'; END IF;
    IF p_action='pause' THEN
        v_question_text:=p_payload->>'question';
        IF COALESCE(length(v_question_text),0) NOT BETWEEN 1 AND 16000 THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        IF v_run.status='waiting_for_input' AND v_state.pending_clarification=jsonb_build_object('id',v_question,'question',v_question_text) THEN
            RETURN jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'events','[]'::jsonb);
        END IF;
        v_run:=trellis_private.require_run(v_run.id,p_capability);
        IF v_run.generation IS DISTINCT FROM (p_payload->>'generation')::integer OR v_run.status<>'running'
           OR v_run.cancel_requested_at IS NOT NULL OR v_run.deadline_at<=v_now OR v_state.budget_unknown
           OR EXISTS(SELECT 1 FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id AND status IN ('pending','streaming'))
           OR EXISTS(SELECT 1 FROM trellis.tool_calls WHERE user_id=v_user AND run_id=v_run.id AND status IN ('pending','running')) THEN
            RAISE EXCEPTION 'research_not_quiescent' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.research_runs SET phase='waiting_for_input',revision=revision+1,
            paused_remaining_seconds=GREATEST(0,extract(epoch FROM v_run.deadline_at-v_now)),
            pending_clarification=jsonb_build_object('id',v_question,'question',v_question_text),updated_at=v_now
            WHERE user_id=v_user AND run_id=v_run.id;
        UPDATE trellis.runs SET status='waiting_for_input',lease_token=NULL,lease_expires_at=NULL
            WHERE user_id=v_user AND id=v_run.id RETURNING * INTO v_run;
        v_event:=trellis_private.append_event(v_user,v_run.id,'research.clarification_requested',jsonb_build_object('phase','waiting_for_input',
            'status','waiting_for_input','clarification_id',v_question,'question',v_question_text));
        SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id;
        RETURN jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'events',jsonb_build_array(v_event));
    END IF;

    v_op_id:=(p_payload->>'operation_id')::uuid;
    v_text:=btrim(p_payload->>'text');
    IF v_op_id IS NULL OR COALESCE(length(v_text),0) NOT BETWEEN 1 AND 16000 THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    v_fingerprint:=encode(sha256(convert_to(jsonb_build_object('action',p_action,'payload',p_payload)::text,'UTF8')),'hex');
    SELECT * INTO v_op FROM trellis.research_operations WHERE user_id=v_user AND run_id=v_run.id AND operation_id=v_op_id;
    IF FOUND THEN
        IF v_op.action<>p_action OR v_op.fingerprint<>v_fingerprint THEN
            RAISE EXCEPTION 'research_operation_conflict' USING ERRCODE='22023';
        END IF;
        RETURN v_op.receipt||jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),
            'lease_acquired',v_run.status='queued' AND v_run.lease_token=trellis_private.capability_digest(p_capability) AND v_run.lease_expires_at>v_now);
    END IF;
    IF v_run.status<>'waiting_for_input' OR v_run.cancel_requested_at IS NOT NULL
       OR v_state.pending_clarification->>'id' IS DISTINCT FROM v_question::text OR v_state.budget_unknown
       OR v_state.paused_remaining_seconds IS NULL OR v_state.paused_remaining_seconds<=0 THEN
        RAISE EXCEPTION 'research_clarification_stale' USING ERRCODE='22023';
    END IF;
    UPDATE trellis.research_runs SET phase='scoping',pending_clarification=NULL,paused_remaining_seconds=NULL,
        checkpoint=jsonb_set(checkpoint,'{clarifications}',COALESCE(checkpoint->'clarifications','[]'::jsonb)||
            jsonb_build_array(jsonb_build_object('id',v_question,'question',v_state.pending_clarification->>'question','answer',v_text))),
        revision=revision+1,updated_at=v_now WHERE user_id=v_user AND run_id=v_run.id;
    UPDATE trellis.research_tasks SET status='queued',result=NULL,revision=revision+1,updated_at=v_now
        WHERE user_id=v_user AND run_id=v_run.id AND role='lead' AND status='completed';
    UPDATE trellis.runs SET status='queued',generation=generation+1,
        deadline_at=v_now+make_interval(secs=>v_state.paused_remaining_seconds::double precision),
        lease_token=trellis_private.capability_digest(p_capability),lease_expires_at=v_now+interval '60 seconds'
        WHERE user_id=v_user AND id=v_run.id RETURNING * INTO v_run;
    v_event:=trellis_private.append_event(v_user,v_run.id,'research.clarification_answered',jsonb_build_object('phase','scoping','status','queued',
        'clarification_id',v_question,'clarification_answered',true));
    SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id;
    v_result:=jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'events',jsonb_build_array(v_event),'lease_acquired',true);
    INSERT INTO trellis.research_operations(user_id,run_id,operation_id,action,fingerprint,receipt)
        VALUES(v_user,v_run.id,v_op_id,p_action,v_fingerprint,v_result);
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.research_mutate_before_clarification(text,jsonb,text) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION trellis_private.research_mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.research_mutate(text,jsonb,text) TO authenticated;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_research_clarification;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE; v_event jsonb;
BEGIN
    IF p_action IN ('request_run_cancellation','request_recovery_cancellation') THEN
        SELECT * INTO v_run FROM trellis.runs WHERE user_id=v_user AND id=(p_payload->>'run_id')::uuid FOR UPDATE;
        IF FOUND AND v_run.status='waiting_for_input' THEN
            UPDATE trellis.research_runs SET phase='stopped',stop_reason='user_cancelled',pending_clarification=NULL,updated_at=clock_timestamp()
                WHERE user_id=v_user AND run_id=v_run.id;
            UPDATE trellis.runs SET status='cancelled',cancel_requested_at=clock_timestamp(),finished_at=clock_timestamp(),
                stop_reason='user_cancelled',lease_token=NULL,lease_expires_at=NULL
                WHERE user_id=v_user AND id=v_run.id RETURNING * INTO v_run;
            v_event:=trellis_private.append_event(v_user,v_run.id,'run.cancelled',jsonb_build_object('status','cancelled','code','user_cancelled'));
            SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id;
            RETURN jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'event',v_event);
        END IF;
    END IF;
    RETURN trellis_private.mutate_before_research_clarification(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_research_clarification(text,jsonb,text) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
