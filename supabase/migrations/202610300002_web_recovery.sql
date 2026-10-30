BEGIN;

ALTER TABLE trellis.tool_calls DROP CONSTRAINT tool_calls_recovery_policy_check;
ALTER TABLE trellis.tool_calls ADD CONSTRAINT tool_calls_recovery_policy_check
    CHECK (recovery_policy IN ('read_only','patch','command','never','checkpointed'));

CREATE OR REPLACE FUNCTION trellis_private.preserve_uncertain_tools() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_uncertain uuid;
BEGIN
    UPDATE trellis.tool_calls SET status='cancelled',finished_at=COALESCE(finished_at,NEW.finished_at,now()),
        result_source=COALESCE(result_source,'checkpointed_run_stopped')
    WHERE user_id=NEW.user_id AND run_id=NEW.id AND recovery_policy='checkpointed'
        AND status IN ('pending','running','outcome_unknown');
    UPDATE trellis.tool_calls SET status='outcome_unknown',finished_at=COALESCE(finished_at,NEW.finished_at,now()),
        result_source=COALESCE(result_source,'run_terminated_after_dispatch')
    WHERE user_id=NEW.user_id AND run_id=NEW.id
        AND (status='running' OR (status='pending' AND dispatch_attempt>0 AND retry_generation IS NOT NULL))
        AND COALESCE(recovery_policy,CASE WHEN name IN ('list_files','search_files','read_file','inspect_git')
            THEN 'read_only' ELSE 'never' END) NOT IN ('read_only','checkpointed');
    SELECT id INTO v_uncertain FROM trellis.tool_calls WHERE user_id=NEW.user_id AND run_id=NEW.id
        AND status='outcome_unknown' AND COALESCE(recovery_policy,CASE
            WHEN name IN ('list_files','search_files','read_file','inspect_git') THEN 'read_only' ELSE 'never' END) NOT IN ('read_only','checkpointed')
        ORDER BY created_at,id LIMIT 1;
    IF v_uncertain IS NOT NULL THEN
        NEW.retry_allowed:=false;
        NEW.uncertain_tool_call_id:=v_uncertain;
        NEW.recovery_reason:=COALESCE(NEW.recovery_reason,'tool_outcome_unknown');
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION trellis_private.mutate_before_atomic_generation(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_now timestamptz:=clock_timestamp();
    v_run trellis.runs%ROWTYPE; v_result jsonb; v_event jsonb; v_limit integer;
    v_previous text; v_owned boolean; v_reason text; v_uncertain uuid; v_cancel boolean;
BEGIN
    IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' THEN
        RAISE EXCEPTION 'mutation payload must be an object' USING ERRCODE='22023';
    END IF;
    IF p_action='create_run' THEN
        PERFORM 1 FROM trellis.chats WHERE id=(p_payload->>'session_id')::uuid AND user_id=v_user FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'session not found' USING ERRCODE='22023'; END IF;
        SELECT * INTO v_run FROM trellis.runs WHERE user_id=v_user AND chat_id=(p_payload->>'session_id')::uuid
            AND turn_id=p_payload->>'turn_id' ORDER BY created_at DESC,id DESC LIMIT 1;
        IF FOUND AND NOT v_run.retry_allowed
           AND v_run.client_request_id IS DISTINCT FROM p_payload->>'client_request_id' THEN
            RAISE EXCEPTION 'unsafe_retry_requires_review' USING ERRCODE='22023';
        END IF;
        IF FOUND AND v_run.status='recovering' THEN
            IF v_run.request_fingerprint IS NOT NULL AND v_run.request_fingerprint IS DISTINCT FROM p_payload->>'request_fingerprint' THEN
                RAISE EXCEPTION 'run request conflicts with a previous payload' USING ERRCODE='22023';
            END IF;
            v_owned:=v_run.lease_token=trellis_private.capability_digest(p_capability) AND v_run.lease_expires_at>v_now;
            RETURN jsonb_build_object('run',to_jsonb(v_run)-'lease_token'-'request_fingerprint',
                'lease_acquired',COALESCE(v_owned,false));
        END IF;
        IF p_payload->'recovery_contract' IS NOT NULL AND p_payload->'recovery_contract'<>'null'::jsonb THEN
            PERFORM trellis_private.validate_recovery_contract(p_payload->'recovery_contract',p_payload->>'client_request_id');
        END IF;
        v_previous:=current_setting('trellis.recovery_contract',true);
        PERFORM set_config('trellis.recovery_contract',COALESCE(NULLIF(p_payload->'recovery_contract','null'::jsonb)::text,''),true);
        v_result:=trellis_private.mutate_before_recovery_contracts(p_action,p_payload,p_capability);
        PERFORM set_config('trellis.recovery_contract',COALESCE(v_previous,''),true);
        RETURN v_result;
    ELSIF p_action='list_recovery_candidates' THEN
        v_limit:=COALESCE((p_payload->>'limit')::integer,20);
        IF v_limit NOT BETWEEN 1 AND 20 OR p_payload->>'installation_id' IS NULL THEN
            RAISE EXCEPTION 'invalid recovery discovery bounds' USING ERRCODE='22023';
        END IF;
        SELECT COALESCE(jsonb_agg(to_jsonb(r)-'lease_token'-'request_fingerprint' ORDER BY r.created_at,r.id),'[]'::jsonb)
        INTO v_result FROM (
            SELECT * FROM trellis.runs WHERE user_id=v_user AND recovery_contract IS NOT NULL
                AND recovery_contract->>'installation_id'=p_payload->>'installation_id'
                AND status IN ('queued','recovering','running','waiting_for_approval','cancelling')
                AND (lease_expires_at IS NULL OR lease_expires_at<=v_now)
            ORDER BY created_at,id LIMIT v_limit
        ) r;
        RETURN v_result;
    ELSIF p_action='claim_recovery' THEN
        SELECT * INTO v_run FROM trellis.runs WHERE user_id=v_user AND id=(p_payload->>'run_id')::uuid FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
        IF v_run.recovery_contract IS NULL THEN v_reason:='legacy_run';
        ELSIF v_run.recovery_contract->>'installation_id' IS DISTINCT FROM p_payload->>'installation_id' THEN v_reason:='installation_mismatch';
        ELSIF v_run.status NOT IN ('queued','recovering','running','waiting_for_approval','cancelling') THEN v_reason:='terminal_run';
        ELSIF v_run.lease_expires_at>v_now THEN
            v_owned:=v_run.lease_token=trellis_private.capability_digest(p_capability);
            RETURN jsonb_build_object('run',to_jsonb(v_run)-'lease_token'-'request_fingerprint',
                'lease_acquired',COALESCE(v_owned,false),'rejection_reason',CASE WHEN v_owned THEN NULL ELSE 'lease_active' END);
        ELSIF v_run.generation IS DISTINCT FROM (p_payload->>'expected_generation')::integer THEN v_reason:='generation_changed';
        ELSIF v_run.recovery_count>=3 THEN
            v_reason:=CASE WHEN v_run.cancel_requested_at IS NOT NULL OR v_run.status='cancelling' THEN 'user_cancelled' ELSE 'recovery_limit' END;
        ELSIF v_run.deadline_at<=v_now AND v_run.cancel_requested_at IS NULL THEN v_reason:='deadline_exceeded';
        END IF;
        IF v_reason IN ('recovery_limit','deadline_exceeded','user_cancelled') THEN
            UPDATE trellis.tool_calls SET status=CASE WHEN recovery_policy='checkpointed' THEN 'cancelled' ELSE 'outcome_unknown' END,finished_at=v_now,
                result_source='unresolved_on_recovery_stop'
            WHERE user_id=v_user AND run_id=v_run.id AND status='running';
            SELECT id INTO v_uncertain FROM trellis.tool_calls
            WHERE user_id=v_user AND run_id=v_run.id AND status='outcome_unknown'
                AND COALESCE(recovery_policy,CASE WHEN name IN ('list_files','search_files','read_file','inspect_git')
                    THEN 'read_only' ELSE 'never' END) NOT IN ('read_only','checkpointed')
            ORDER BY created_at,id LIMIT 1;
            UPDATE trellis.model_calls SET error_code=COALESCE(error_code,'model_outcome_unknown')
            WHERE user_id=v_user AND run_id=v_run.id AND status='streaming';
            v_cancel:=v_reason='user_cancelled';
            UPDATE trellis.runs SET status=CASE WHEN v_cancel THEN 'cancelled' ELSE 'interrupted' END,
                finished_at=v_now,lease_token=NULL,lease_expires_at=NULL,waiting_tool_call_id=NULL,
                recovery_reason=v_reason,stop_reason=v_reason,error_code=v_reason,
                error_message=CASE WHEN v_cancel THEN 'The run was cancelled.'
                    WHEN v_reason='recovery_limit' THEN 'The run reached its recovery takeover limit.'
                    ELSE 'The original run deadline expired.' END,
                retry_allowed=retry_allowed AND v_uncertain IS NULL,uncertain_tool_call_id=v_uncertain
            WHERE user_id=v_user AND id=v_run.id RETURNING * INTO v_run;
            PERFORM trellis_private.append_event(v_user,v_run.id,CASE WHEN v_cancel THEN 'run.cancelled' ELSE 'run.interrupted' END,
                jsonb_build_object('status',v_run.status,'code',v_reason,'message',v_run.error_message,
                    'retry_allowed',v_run.retry_allowed,'uncertain_tool_call_id',v_uncertain));
            SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id AND user_id=v_user;
        END IF;
        IF v_reason IS NOT NULL THEN
            RETURN jsonb_build_object('run',to_jsonb(v_run)-'lease_token'-'request_fingerprint',
                'lease_acquired',false,'rejection_reason',v_reason);
        END IF;
        IF v_run.lease_token=trellis_private.capability_digest(p_capability) THEN
            RAISE EXCEPTION 'recovery requires a new ownership capability' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.runs SET lease_token=trellis_private.capability_digest(p_capability),
            lease_expires_at=v_now+interval '60 seconds',generation=generation+1,recovery_count=recovery_count+1,
            status=CASE WHEN cancel_requested_at IS NOT NULL OR status='cancelling' THEN 'cancelling' ELSE 'recovering' END,
            recovery_reason=CASE WHEN cancel_requested_at IS NOT NULL OR status='cancelling' THEN 'cancellation_requested' ELSE NULL END
        WHERE user_id=v_user AND id=v_run.id RETURNING * INTO v_run;
        v_event:=trellis_private.append_event(v_user,v_run.id,'run.recovering',jsonb_build_object(
            'status',v_run.status,'generation',v_run.generation,'recovery_count',v_run.recovery_count));
        RETURN jsonb_build_object('run',to_jsonb(v_run)-'lease_token'-'request_fingerprint',
            'lease_acquired',true,'event',v_event);
    ELSIF p_action='relinquish_run' THEN
        v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        IF v_run.recovery_contract IS NULL OR v_run.status NOT IN ('queued','recovering','running','waiting_for_approval','cancelling') THEN
            RAISE EXCEPTION 'run is not recoverable' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.runs SET lease_expires_at=v_now WHERE id=v_run.id AND user_id=v_user RETURNING * INTO v_run;
        RETURN to_jsonb(v_run)-'lease_token'-'request_fingerprint';
    ELSIF p_action='finish_recovery' THEN
        v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        IF v_run.status<>'recovering' OR v_run.cancel_requested_at IS NOT NULL THEN
            RAISE EXCEPTION 'run is not recovering' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.runs SET status=CASE WHEN waiting_tool_call_id IS NULL THEN 'running' ELSE 'waiting_for_approval' END
            WHERE id=v_run.id AND user_id=v_user RETURNING * INTO v_run;
        v_event:=trellis_private.append_event(v_user,v_run.id,'run.resumed',jsonb_build_object('status',v_run.status,'generation',v_run.generation));
        RETURN jsonb_build_object('run',to_jsonb(v_run)-'lease_token'-'request_fingerprint','event',v_event);
    ELSIF p_action='renew_run_lease' THEN
        v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        IF v_run.status='recovering' THEN
            UPDATE trellis.runs SET lease_expires_at=v_now+interval '60 seconds' WHERE id=v_run.id AND user_id=v_user RETURNING * INTO v_run;
            RETURN jsonb_build_object('run',to_jsonb(v_run)-'lease_token'-'request_fingerprint','lease_acquired',true);
        END IF;
    ELSIF p_action='recover_expired_run' THEN
        SELECT * INTO v_run FROM trellis.runs WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user FOR UPDATE;
        IF FOUND AND v_run.recovery_contract IS NOT NULL THEN RETURN to_jsonb(v_run)-'lease_token'-'request_fingerprint'; END IF;
    ELSIF p_action IN ('set_session_workspace','set_session_model','claim_turn','add_user_message','add_assistant_message') THEN
        IF EXISTS(SELECT 1 FROM trellis.runs WHERE user_id=v_user AND chat_id=(p_payload->>'session_id')::uuid AND status='recovering') THEN
            RAISE EXCEPTION 'session_run_busy' USING ERRCODE='PT409';
        END IF;
    END IF;
    RETURN trellis_private.mutate_before_recovery_contracts(p_action,p_payload,p_capability);
END;
$$;

CREATE OR REPLACE FUNCTION trellis_private.mutate_before_local_reconciliation(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE; v_tool trellis.tool_calls%ROWTYPE;
    v_now timestamptz:=clock_timestamp(); v_id uuid; v_attempt integer; v_policy text;
    v_fingerprint text; v_digest text; v_args_hash text; v_snapshot jsonb; v_result jsonb; v_event jsonb;
BEGIN
    IF p_action NOT IN ('begin_tool_execution','record_tool_result','mark_tool_outcome_unknown') THEN
        IF p_action='commit_generation' AND p_payload->>'final_content' IS NOT NULL THEN
            SELECT * INTO v_run FROM trellis.runs WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
            IF NOT EXISTS(SELECT 1 FROM trellis.model_calls WHERE id=(p_payload->>'call_id')::uuid
                AND run_id=v_run.id AND user_id=v_user AND completion_receipt IS NOT NULL) THEN
                PERFORM trellis_private.require_run(v_run.id,p_capability);
                IF EXISTS(SELECT 1 FROM trellis.tool_calls WHERE run_id=v_run.id AND user_id=v_user
                    AND status IN ('pending','running','outcome_unknown')) THEN
                    RAISE EXCEPTION 'final generation has unfinished tool operations' USING ERRCODE='22023';
                END IF;
            END IF;
        END IF;
        v_result:=trellis_private.mutate_before_tool_dispatch(p_action,p_payload,p_capability);
        IF jsonb_typeof(v_result)='object' THEN
            IF v_result ? 'tool_call' THEN
                v_result:=jsonb_set(v_result,'{tool_call}',trellis_private.public_tool_call(v_result->'tool_call'));
            END IF;
            IF jsonb_typeof(v_result->'tool_calls')='array' THEN
                v_result:=jsonb_set(v_result,'{tool_calls}',COALESCE((SELECT jsonb_agg(
                    trellis_private.public_tool_call(t.value) ORDER BY t.ordinality)
                    FROM jsonb_array_elements(v_result->'tool_calls') WITH ORDINALITY t(value,ordinality)),'[]'::jsonb));
            END IF;
        END IF;
        RETURN v_result;
    END IF;
    IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' OR octet_length(p_payload::text)>1048576 THEN
        RAISE EXCEPTION 'invalid tool operation' USING ERRCODE='22023';
    END IF;
    v_digest:=trellis_private.capability_digest(p_capability);
    v_fingerprint:=encode(sha256(convert_to(jsonb_build_object('action',p_action,'payload',p_payload)::text,'UTF8')),'hex');
    SELECT * INTO v_run FROM trellis.runs WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
    SELECT * INTO v_tool FROM trellis.tool_calls WHERE id=(p_payload->>'tool_call_id')::uuid
        AND run_id=v_run.id AND user_id=v_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'tool call not found' USING ERRCODE='22023'; END IF;

    IF p_action='record_tool_result' THEN
        IF v_tool.result_receipt IS NOT NULL THEN
            IF v_tool.result_owner_digest IS DISTINCT FROM v_digest THEN
                RAISE EXCEPTION 'tool result receipt ownership invalid' USING ERRCODE='42501';
            END IF;
            IF v_tool.result_fingerprint IS DISTINCT FROM v_fingerprint THEN
                RAISE EXCEPTION 'tool result conflicts with saved content' USING ERRCODE='22023';
            END IF;
            RETURN v_tool.result_receipt;
        END IF;
        v_run:=trellis_private.require_run(v_run.id,p_capability);
        IF v_tool.status='pending' AND COALESCE(p_payload->>'status','completed')='completed' AND v_run.recovery_contract IS NOT NULL THEN
            RAISE EXCEPTION 'tool result has no recorded dispatch' USING ERRCODE='22023';
        END IF;
        v_result:=trellis_private.mutate_before_tool_dispatch(p_action,p_payload,p_capability);
        UPDATE trellis.tool_calls SET result_source=COALESCE(p_payload->>'result_source','executor')
            WHERE id=v_tool.id AND user_id=v_user RETURNING * INTO v_tool;
        v_result:=jsonb_set(v_result,'{tool_call}',trellis_private.public_tool_call(to_jsonb(v_tool)));
        UPDATE trellis.run_events SET data=data||jsonb_build_object('result_source',v_tool.result_source)
            WHERE user_id=v_user AND run_id=v_run.id AND sequence=(v_result#>>'{event,sequence}')::integer
            RETURNING to_jsonb(trellis.run_events.*) INTO v_event;
        v_result:=jsonb_set(v_result,'{event}',v_event);
        UPDATE trellis.tool_calls SET result_fingerprint=v_fingerprint,result_owner_digest=v_digest,result_receipt=v_result
            WHERE id=v_tool.id AND user_id=v_user;
        RETURN v_result;
    END IF;

    v_run:=trellis_private.require_run(v_run.id,p_capability);
    v_policy:=COALESCE(v_tool.recovery_policy,CASE
        WHEN v_tool.name IN ('list_files','search_files','read_file','inspect_git') THEN 'read_only'
        WHEN v_tool.name IN ('web_search','web_read','web_crawl','web_source') THEN 'checkpointed'
        WHEN v_tool.name='apply_patch' THEN 'patch' WHEN v_tool.name='run_command' THEN 'command' ELSE 'never' END);
    IF p_action='mark_tool_outcome_unknown' THEN
        IF v_run.status NOT IN ('running','recovering','cancelling') OR v_tool.status NOT IN ('running','outcome_unknown')
           OR COALESCE(length(p_payload->>'reason'),0) NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'tool has no dispatched uncertain outcome' USING ERRCODE='22023';
        END IF;
        IF v_tool.status='outcome_unknown' THEN
            IF v_tool.result_source IS DISTINCT FROM p_payload->>'reason' THEN
                RAISE EXCEPTION 'uncertain tool reason conflicts with saved content' USING ERRCODE='22023';
            END IF;
            RETURN trellis_private.public_tool_call(to_jsonb(v_tool));
        END IF;
        UPDATE trellis.tool_calls SET status=CASE WHEN v_policy='checkpointed' THEN 'cancelled' ELSE 'outcome_unknown' END,
            finished_at=v_now,result_source=p_payload->>'reason'
            WHERE id=v_tool.id AND user_id=v_user RETURNING * INTO v_tool;
        IF v_policy NOT IN ('read_only','checkpointed') THEN
            UPDATE trellis.runs SET retry_allowed=false,uncertain_tool_call_id=COALESCE(uncertain_tool_call_id,v_tool.id),
                recovery_reason=COALESCE(recovery_reason,'tool_outcome_unknown') WHERE id=v_run.id AND user_id=v_user;
        END IF;
        RETURN trellis_private.public_tool_call(to_jsonb(v_tool));
    END IF;

    IF v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL OR v_run.waiting_tool_call_id IS NOT NULL THEN
        RAISE EXCEPTION 'run not ready for tool dispatch' USING ERRCODE='22023';
    END IF;
    v_id:=(p_payload->>'dispatch_id')::uuid;
    v_attempt:=(p_payload->>'expected_attempt')::integer;
    v_args_hash:=encode(sha256(convert_to(v_tool.arguments::text,'UTF8')),'hex');
    IF v_id IS NULL OR v_attempt IS NULL OR v_attempt<0
       OR p_payload->>'recovery_policy' IS DISTINCT FROM v_policy THEN
        RAISE EXCEPTION 'invalid tool dispatch identity or policy' USING ERRCODE='22023';
    END IF;
    IF v_tool.dispatch_id=v_id AND v_tool.dispatch_receipt IS NOT NULL THEN
        IF v_tool.dispatch_owner_digest IS DISTINCT FROM v_digest
           OR v_tool.dispatch_generation IS DISTINCT FROM v_run.generation THEN
            RAISE EXCEPTION 'tool dispatch ownership changed' USING ERRCODE='42501';
        END IF;
        IF v_tool.dispatch_fingerprint IS DISTINCT FROM v_fingerprint OR v_tool.dispatch_arguments_hash IS DISTINCT FROM v_args_hash THEN
            RAISE EXCEPTION 'tool dispatch conflicts with saved content' USING ERRCODE='22023';
        END IF;
        IF v_tool.status='running' THEN RETURN v_tool.dispatch_receipt; END IF;
        RETURN jsonb_set(v_tool.dispatch_receipt,'{tool_call}',trellis_private.public_tool_call(to_jsonb(v_tool)));
    END IF;
    IF v_attempt<>v_tool.dispatch_attempt THEN
        RAISE EXCEPTION 'tool dispatch attempt changed' USING ERRCODE='22023';
    END IF;
    IF v_tool.dispatch_attempt=0 THEN
        IF v_tool.status<>'pending' THEN RAISE EXCEPTION 'tool is not prepared' USING ERRCODE='22023'; END IF;
    ELSE
        IF v_policy='patch' THEN RAISE EXCEPTION 'patch_reconciliation_required' USING ERRCODE='22023'; END IF;
        IF v_policy<>'read_only' OR v_tool.status<>'running' OR v_tool.dispatch_attempt>=2
           OR v_run.generation<=COALESCE(v_tool.dispatch_generation,v_run.generation) THEN
            RAISE EXCEPTION 'tool dispatch cannot be repeated' USING ERRCODE='22023';
        END IF;
    END IF;
    IF v_tool.name IN ('apply_patch','run_command') AND
       (v_tool.approval_decision IS DISTINCT FROM 'approved' OR v_tool.approval_preview IS NULL) THEN
        RAISE EXCEPTION 'tool dispatch requires original approval' USING ERRCODE='22023';
    END IF;
    v_snapshot:=p_payload->'input_snapshot';
    IF v_snapshot IS NULL OR v_snapshot='null'::jsonb THEN
        v_snapshot:=jsonb_build_object('arguments',v_tool.arguments,'approval_preview',v_tool.approval_preview);
    END IF;
    IF jsonb_typeof(v_snapshot) IS DISTINCT FROM 'object' OR octet_length(v_snapshot::text)>262144
       OR v_snapshot->'arguments' IS DISTINCT FROM v_tool.arguments
       OR COALESCE(NULLIF(v_snapshot->'approval_preview','null'::jsonb),'null'::jsonb)
           IS DISTINCT FROM COALESCE(v_tool.approval_preview,'null'::jsonb)
       OR (v_tool.dispatch_arguments_hash IS NOT NULL AND v_tool.dispatch_arguments_hash IS DISTINCT FROM v_args_hash) THEN
        RAISE EXCEPTION 'tool dispatch input changed' USING ERRCODE='22023';
    END IF;
    UPDATE trellis.tool_calls SET status='running',dispatch_attempt=dispatch_attempt+1,dispatch_id=v_id,
        dispatch_generation=v_run.generation,dispatched_at=v_now,recovery_policy=v_policy,input_snapshot=v_snapshot,
        dispatch_arguments_hash=v_args_hash,dispatch_fingerprint=v_fingerprint,dispatch_owner_digest=v_digest
    WHERE id=v_tool.id AND user_id=v_user RETURNING * INTO v_tool;
    v_event:=trellis_private.append_event(v_user,v_run.id,'run.phase',jsonb_build_object('phase','tool_use',
        'tool_call_id',v_tool.id,'dispatch_id',v_id,'attempt',v_tool.dispatch_attempt,'generation',v_run.generation));
    v_result:=jsonb_build_object('tool_call',trellis_private.public_tool_call(to_jsonb(v_tool)),'event',v_event);
    UPDATE trellis.tool_calls SET dispatch_receipt=v_result WHERE id=v_tool.id AND user_id=v_user;
    RETURN v_result;
END;
$$;

CREATE OR REPLACE FUNCTION trellis_private.mutate_before_recovery_review(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE; v_tool trellis.tool_calls%ROWTYPE;
    v_fingerprint text; v_digest text; v_proof jsonb:=p_payload->'proof'; v_result jsonb; v_event jsonb;
    v_snapshot jsonb; v_args_hash text; v_id uuid; v_attempt integer;
BEGIN
    IF p_action NOT IN ('authorize_tool_retry','begin_tool_execution','record_tool_result','mark_tool_outcome_unknown') THEN
        RETURN trellis_private.mutate_before_local_reconciliation(p_action,p_payload,p_capability);
    END IF;
    IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' OR octet_length(p_payload::text)>2097152 THEN
        RAISE EXCEPTION 'invalid reconciliation operation' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
    SELECT * INTO v_tool FROM trellis.tool_calls WHERE id=(p_payload->>'tool_call_id')::uuid
        AND run_id=v_run.id AND user_id=v_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'tool call not found' USING ERRCODE='22023'; END IF;
    IF p_action='mark_tool_outcome_unknown' THEN
        IF v_tool.status='pending' AND v_tool.dispatch_attempt>0 AND v_tool.retry_generation IS NOT NULL THEN
            PERFORM trellis_private.require_run(v_run.id,p_capability);
            UPDATE trellis.tool_calls SET status='running' WHERE id=v_tool.id AND user_id=v_user;
        END IF;
        RETURN trellis_private.mutate_before_local_reconciliation(p_action,p_payload,p_capability);
    END IF;
    IF p_action='record_tool_result' THEN
        v_digest:=trellis_private.capability_digest(p_capability);
        v_fingerprint:=encode(sha256(convert_to(jsonb_build_object('action',p_action,'payload',p_payload)::text,'UTF8')),'hex');
        IF v_tool.result_receipt IS NOT NULL THEN
            IF v_tool.result_owner_digest IS DISTINCT FROM v_digest THEN
                RAISE EXCEPTION 'tool result receipt ownership invalid' USING ERRCODE='42501';
            END IF;
            IF v_tool.result_fingerprint IS DISTINCT FROM v_fingerprint THEN
                RAISE EXCEPTION 'tool result conflicts with saved content' USING ERRCODE='22023';
            END IF;
            RETURN v_tool.result_receipt;
        END IF;
        v_snapshot:=NULLIF(p_payload->'result_snapshot','null'::jsonb);
        IF v_tool.recovery_policy='checkpointed' AND v_tool.dispatch_attempt>0 AND (
            v_snapshot->>'source' IS DISTINCT FROM 'checkpointed_web'
            OR v_snapshot->>'dispatch_id' IS DISTINCT FROM v_tool.dispatch_id::text
            OR v_snapshot->>'generation' IS DISTINCT FROM v_tool.dispatch_generation::text
            OR v_tool.dispatch_generation>v_run.generation
            OR (COALESCE(p_payload->>'status','completed') NOT IN ('cancelled','timed_out')
                AND v_tool.dispatch_generation IS DISTINCT FROM v_run.generation)
            OR v_snapshot->>'content_sha256' IS DISTINCT FROM encode(sha256(convert_to(p_payload->>'content','UTF8')),'hex')) THEN
            RAISE EXCEPTION 'checkpointed result snapshot invalid' USING ERRCODE='22023';
        END IF;
        IF v_snapshot IS NOT NULL AND (jsonb_typeof(v_snapshot)<>'object' OR octet_length(v_snapshot::text)>1048576) THEN
            RAISE EXCEPTION 'invalid tool result snapshot' USING ERRCODE='22023';
        END IF;
        IF v_tool.status='pending' AND v_tool.dispatch_attempt>0
           AND v_tool.recovery_policy NOT IN ('read_only','checkpointed')
           AND COALESCE(p_payload->>'result_source','executor')<>'reconciled_desired_state' THEN
            RAISE EXCEPTION 'dispatched tool requires reconciliation before recording a result' USING ERRCODE='22023';
        END IF;
        IF p_payload->>'result_source'='reconciled_desired_state' THEN
            PERFORM trellis_private.require_run(v_run.id,p_capability);
            IF NOT (v_tool.status='running' OR (v_tool.status='pending' AND v_tool.dispatch_attempt>0 AND v_tool.retry_generation IS NOT NULL))
               OR v_run.generation<=COALESCE(v_tool.dispatch_generation,v_run.generation)
               OR COALESCE(p_payload->>'status','completed')<>'completed'
               OR v_snapshot->>'source' IS DISTINCT FROM 'reconciled_desired_state' THEN
                RAISE EXCEPTION 'tool has no recoverable patch result' USING ERRCODE='22023';
            END IF;
            PERFORM trellis_private.validate_patch_reconciliation(v_tool,v_snapshot->'reconciliation_proof','after_sha256');
            IF v_tool.status='pending' THEN
                UPDATE trellis.tool_calls SET status='running' WHERE id=v_tool.id AND user_id=v_user;
            END IF;
        END IF;
        v_result:=trellis_private.mutate_before_local_reconciliation(p_action,p_payload-'result_snapshot',p_capability);
        UPDATE trellis.tool_calls SET result_snapshot=v_snapshot WHERE id=v_tool.id AND user_id=v_user RETURNING * INTO v_tool;
        v_result:=jsonb_set(v_result,'{tool_call}',trellis_private.public_tool_call(to_jsonb(v_tool)));
        UPDATE trellis.tool_calls SET result_receipt=v_result,result_fingerprint=v_fingerprint WHERE id=v_tool.id AND user_id=v_user;
        RETURN v_result;
    END IF;
    IF p_action='begin_tool_execution' AND (v_tool.dispatch_attempt=0 OR v_tool.dispatch_id=(p_payload->>'dispatch_id')::uuid) THEN
        RETURN trellis_private.mutate_before_local_reconciliation(p_action,p_payload,p_capability);
    END IF;
    v_run:=trellis_private.require_run(v_run.id,p_capability);
    IF v_run.status NOT IN ('running','recovering') OR v_run.cancel_requested_at IS NOT NULL OR v_run.waiting_tool_call_id IS NOT NULL THEN
        RAISE EXCEPTION 'run not ready for tool reconciliation' USING ERRCODE='22023';
    END IF;
    v_digest:=trellis_private.capability_digest(p_capability);
    v_fingerprint:=encode(sha256(convert_to(jsonb_build_object('action',p_action,'payload',p_payload)::text,'UTF8')),'hex');
    IF p_action='authorize_tool_retry' THEN
        IF v_tool.retry_receipt IS NOT NULL AND v_tool.retry_fingerprint=v_fingerprint
           AND v_tool.retry_generation=v_run.generation AND v_tool.retry_owner_digest=v_digest THEN
            IF v_tool.status='pending' THEN RETURN v_tool.retry_receipt; END IF;
            RETURN trellis_private.public_tool_call(to_jsonb(v_tool));
        END IF;
        IF v_tool.status NOT IN ('running','pending') OR v_tool.dispatch_attempt<1
           OR (v_tool.status='pending' AND (v_tool.retry_generation IS NULL OR v_tool.retry_generation>=v_run.generation))
           OR v_tool.dispatch_id IS DISTINCT FROM (p_payload->>'expected_dispatch_id')::uuid
           OR v_tool.dispatch_attempt IS DISTINCT FROM (p_payload->>'expected_attempt')::integer
           OR v_run.generation<=COALESCE(v_tool.dispatch_generation,v_run.generation)
           OR v_tool.dispatch_arguments_hash IS DISTINCT FROM encode(sha256(convert_to(v_tool.arguments::text,'UTF8')),'hex') THEN
            RAISE EXCEPTION 'tool retry source changed' USING ERRCODE='22023';
        END IF;
        IF p_payload->>'reconciliation'='read_retry' THEN
            IF v_tool.recovery_policy IS DISTINCT FROM 'read_only' OR v_tool.dispatch_attempt>=2 THEN
                RAISE EXCEPTION 'read retry limit reached' USING ERRCODE='22023';
            END IF;
        ELSIF p_payload->>'reconciliation'='checkpoint_resume' THEN
            IF v_tool.recovery_policy IS DISTINCT FROM 'checkpointed' OR v_tool.dispatch_attempt>=4 THEN
                RAISE EXCEPTION 'checkpoint resume limit reached' USING ERRCODE='22023';
            END IF;
        ELSIF p_payload->>'reconciliation'='unchanged_original' THEN
            IF v_tool.dispatch_attempt>=4 THEN RAISE EXCEPTION 'patch retry limit reached' USING ERRCODE='22023'; END IF;
            PERFORM trellis_private.validate_patch_reconciliation(v_tool,v_proof,'before_sha256');
        ELSE
            RAISE EXCEPTION 'tool cannot be automatically retried' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.tool_calls SET status='pending',retry_generation=v_run.generation,
            retry_fingerprint=v_fingerprint,retry_owner_digest=v_digest,retry_proof=NULLIF(v_proof,'null'::jsonb)
            WHERE id=v_tool.id AND user_id=v_user RETURNING * INTO v_tool;
        PERFORM trellis_private.append_event(v_user,v_run.id,'run.phase',jsonb_build_object('phase','tool_use',
            'tool_call_id',v_tool.id,'reconciliation',p_payload->>'reconciliation',
            'prior_dispatch_id',v_tool.dispatch_id,'attempt',v_tool.dispatch_attempt,'generation',v_run.generation));
        v_result:=trellis_private.public_tool_call(to_jsonb(v_tool));
        UPDATE trellis.tool_calls SET retry_receipt=v_result WHERE id=v_tool.id AND user_id=v_user;
        RETURN v_result;
    END IF;
    IF v_tool.status='running' AND v_tool.recovery_policy='patch' AND v_tool.retry_generation IS NULL THEN
        RAISE EXCEPTION 'patch_reconciliation_required' USING ERRCODE='22023';
    END IF;
    v_id:=(p_payload->>'dispatch_id')::uuid;
    v_attempt:=(p_payload->>'expected_attempt')::integer;
    IF v_run.status<>'running' OR v_tool.status<>'pending' OR v_id IS NULL OR v_id=v_tool.dispatch_id
       OR v_tool.retry_generation IS DISTINCT FROM v_run.generation OR v_tool.retry_owner_digest IS DISTINCT FROM v_digest
       OR v_attempt IS DISTINCT FROM v_tool.dispatch_attempt
       OR p_payload->>'recovery_policy' IS DISTINCT FROM v_tool.recovery_policy
       OR NOT ((v_tool.recovery_policy='read_only' AND v_tool.dispatch_attempt<2)
            OR (v_tool.recovery_policy='checkpointed' AND v_tool.dispatch_attempt<4)
            OR (v_tool.recovery_policy='patch' AND v_tool.dispatch_attempt<4)) THEN
        RAISE EXCEPTION 'tool retry requires current reconciliation' USING ERRCODE='22023';
    END IF;
    IF v_tool.recovery_policy='patch' THEN
        PERFORM trellis_private.validate_patch_reconciliation(v_tool,v_tool.retry_proof,'before_sha256');
    END IF;
    v_snapshot:=NULLIF(p_payload->'input_snapshot','null'::jsonb);
    IF v_snapshot IS NULL THEN v_snapshot:=jsonb_build_object('arguments',v_tool.arguments,'approval_preview',v_tool.approval_preview); END IF;
    v_args_hash:=encode(sha256(convert_to(v_tool.arguments::text,'UTF8')),'hex');
    IF jsonb_typeof(v_snapshot)<>'object' OR octet_length(v_snapshot::text)>262144
       OR v_snapshot->'arguments' IS DISTINCT FROM v_tool.arguments
       OR COALESCE(v_snapshot->'approval_preview','null'::jsonb) IS DISTINCT FROM COALESCE(v_tool.approval_preview,'null'::jsonb)
       OR v_tool.dispatch_arguments_hash IS DISTINCT FROM v_args_hash THEN
        RAISE EXCEPTION 'tool retry input changed' USING ERRCODE='22023';
    END IF;
    UPDATE trellis.tool_calls SET status='running',dispatch_attempt=dispatch_attempt+1,dispatch_id=v_id,
        dispatch_generation=v_run.generation,dispatched_at=clock_timestamp(),input_snapshot=v_snapshot,
        dispatch_fingerprint=v_fingerprint,dispatch_owner_digest=v_digest
        WHERE id=v_tool.id AND user_id=v_user RETURNING * INTO v_tool;
    v_event:=trellis_private.append_event(v_user,v_run.id,'run.phase',jsonb_build_object('phase','tool_use',
        'tool_call_id',v_tool.id,'dispatch_id',v_id,'attempt',v_tool.dispatch_attempt,'generation',v_run.generation));
    v_result:=jsonb_build_object('tool_call',trellis_private.public_tool_call(to_jsonb(v_tool)),'event',v_event);
    UPDATE trellis.tool_calls SET dispatch_receipt=v_result WHERE id=v_tool.id AND user_id=v_user;
    RETURN v_result;
END;
$$;

CREATE FUNCTION trellis.web_readiness() RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
    SELECT jsonb_build_object('schema_version',1,'ready',
        (SELECT count(*)=8 AND bool_and(c.relrowsecurity AND c.relforcerowsecurity)
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='trellis' AND c.relname IN ('web_accounts','web_runs','web_reservations',
                'web_sources','web_crawls','web_crawl_pages','web_citations','web_citation_sets'))
        AND pg_catalog.to_regprocedure('trellis.web_mutate(text,jsonb,text)') IS NOT NULL
        AND EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=pg_catalog.to_regclass('trellis.tool_calls')
            AND c.conname='tool_calls_recovery_policy_check' AND pg_catalog.pg_get_constraintdef(c.oid) LIKE '%checkpointed%')
        AND EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=pg_catalog.to_regclass('trellis.web_runs')
            AND a.attname='reservation_count' AND NOT a.attisdropped)
        AND EXISTS(SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='trellis_private' AND p.proname='mutate_before_recovery_review'
                AND pg_catalog.pg_get_functiondef(p.oid) LIKE '%checkpoint_resume%'
                AND pg_catalog.pg_get_functiondef(p.oid) LIKE '%checkpointed_web%'))
$$;
REVOKE ALL ON FUNCTION trellis.web_readiness() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION trellis.web_readiness() TO anon,authenticated;

COMMIT;
