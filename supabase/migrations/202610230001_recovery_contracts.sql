BEGIN;

ALTER TABLE trellis.runs
    ADD COLUMN recovery_contract jsonb,
    ADD COLUMN generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
    ADD COLUMN retry_allowed boolean NOT NULL DEFAULT true,
    ADD COLUMN recovery_reason text CHECK (length(recovery_reason) BETWEEN 1 AND 100),
    ADD COLUMN uncertain_tool_call_id uuid,
    ADD CONSTRAINT runs_recovery_contract_bounded CHECK (
        recovery_contract IS NULL OR (jsonb_typeof(recovery_contract) = 'object'
            AND octet_length(recovery_contract::text) <= 32768)),
    ADD CONSTRAINT runs_uncertain_tool_owner_fk FOREIGN KEY (user_id,id,uncertain_tool_call_id)
        REFERENCES trellis.tool_calls(user_id,run_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE trellis.runs DROP CONSTRAINT runs_status_check;
ALTER TABLE trellis.runs ADD CONSTRAINT runs_status_check CHECK (status IN (
    'queued','recovering','running','waiting_for_approval','cancelling',
    'completed','failed','cancelled','interrupted'));
DROP INDEX trellis.runs_one_active_per_chat;
CREATE UNIQUE INDEX runs_one_active_per_chat ON trellis.runs(user_id,chat_id)
    WHERE status IN ('queued','recovering','running','waiting_for_approval','cancelling');
DROP INDEX trellis.runs_lease_expiry;
CREATE INDEX runs_lease_expiry ON trellis.runs(lease_expires_at)
    WHERE status IN ('queued','recovering','running','waiting_for_approval','cancelling');
GRANT SELECT (recovery_contract,generation,retry_allowed,recovery_reason,uncertain_tool_call_id)
    ON trellis.runs TO authenticated;

ALTER TABLE trellis.model_calls ADD COLUMN purpose text NOT NULL DEFAULT 'generation'
    CHECK (purpose IN ('generation','summary','native_compaction'));
UPDATE trellis.model_calls SET purpose=request_snapshot->>'purpose'
    WHERE request_snapshot->>'purpose' IN ('summary','native_compaction');
ALTER TABLE trellis.tool_calls
    ADD COLUMN dispatch_attempt integer NOT NULL DEFAULT 0 CHECK (dispatch_attempt BETWEEN 0 AND 2),
    ADD COLUMN dispatch_generation integer CHECK (dispatch_generation >= 0),
    ADD COLUMN dispatched_at timestamptz,
    ADD COLUMN recovery_policy text CHECK (recovery_policy IN ('read_only','patch','command','never')),
    ADD COLUMN result_source text CHECK (length(result_source) BETWEEN 1 AND 100);
ALTER TABLE trellis.tool_calls DROP CONSTRAINT tool_calls_status_check;
ALTER TABLE trellis.tool_calls ADD CONSTRAINT tool_calls_status_check CHECK (status IN (
    'pending','running','completed','failed','denied','cancelled','timed_out','outcome_unknown'));

CREATE FUNCTION trellis_private.validate_recovery_contract(p_contract jsonb,p_request text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_item record; v_workspace jsonb:=p_contract->'workspace';
BEGIN
    IF jsonb_typeof(p_contract) IS DISTINCT FROM 'object' OR octet_length(p_contract::text)>32768
       OR p_contract->'version' IS DISTINCT FROM '1'::jsonb
       OR p_contract->>'runtime_version' IS DISTINCT FROM 'checkpoint-runtime-v1'
       OR p_contract->>'request_identity' IS DISTINCT FROM p_request
       OR p_contract->>'installation_id' IS NULL
       OR jsonb_typeof(p_contract->'adapter_versions') IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_contract->'tool_versions') IS DISTINCT FROM 'object'
       OR NOT p_contract ? 'workspace'
       OR (SELECT count(*) FROM jsonb_object_keys(p_contract))<>7 THEN
        RAISE EXCEPTION 'invalid recovery contract' USING ERRCODE='22023';
    END IF;
    PERFORM (p_contract->>'installation_id')::uuid;
    IF (SELECT count(*) FROM jsonb_each(p_contract->'adapter_versions')) NOT BETWEEN 1 AND 8
       OR (SELECT count(*) FROM jsonb_each(p_contract->'tool_versions'))>32 THEN
        RAISE EXCEPTION 'invalid recovery version bounds' USING ERRCODE='22023';
    END IF;
    FOR v_item IN SELECT * FROM jsonb_each(p_contract->'adapter_versions')
        UNION ALL SELECT * FROM jsonb_each(p_contract->'tool_versions') LOOP
        IF length(v_item.key) NOT BETWEEN 1 AND 100 OR jsonb_typeof(v_item.value)<>'string'
           OR length(v_item.value#>>'{}') NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid recovery version entry' USING ERRCODE='22023';
        END IF;
    END LOOP;
    IF v_workspace IS DISTINCT FROM 'null'::jsonb THEN
        IF jsonb_typeof(v_workspace) IS DISTINCT FROM 'object'
           OR (SELECT count(*) FROM jsonb_object_keys(v_workspace))<>5
           OR v_workspace->>'grant_id' IS NULL
           OR jsonb_typeof(v_workspace->'grant_revision') IS DISTINCT FROM 'number'
           OR v_workspace->>'grant_revision' !~ '^[0-9]+$'
           OR (v_workspace->>'grant_revision')::bigint<1
           OR left(v_workspace->>'canonical_path',1) IS DISTINCT FROM '/'
           OR length(v_workspace->>'canonical_path') NOT BETWEEN 1 AND 4096
           OR jsonb_typeof(v_workspace->'directory_device') IS DISTINCT FROM 'number'
           OR v_workspace->>'directory_device' !~ '^[0-9]+$'
           OR (v_workspace->>'directory_device')::numeric<0
           OR jsonb_typeof(v_workspace->'directory_inode') IS DISTINCT FROM 'number'
           OR v_workspace->>'directory_inode' !~ '^[0-9]+$'
           OR (v_workspace->>'directory_inode')::numeric<0 THEN
            RAISE EXCEPTION 'invalid recovery workspace reference' USING ERRCODE='22023';
        END IF;
        PERFORM (v_workspace->>'grant_id')::uuid;
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.validate_recovery_contract(jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

CREATE FUNCTION trellis_private.protect_recovery_contract() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF TG_OP='UPDATE' AND NEW.recovery_contract IS DISTINCT FROM OLD.recovery_contract THEN
        RAISE EXCEPTION 'recovery contract is immutable' USING ERRCODE='42501';
    END IF;
    IF TG_OP='INSERT' AND NEW.recovery_contract IS NOT NULL THEN
        PERFORM trellis_private.validate_recovery_contract(NEW.recovery_contract,NEW.client_request_id);
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.protect_recovery_contract()
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
CREATE TRIGGER recovery_contract_immutable BEFORE INSERT OR UPDATE OF recovery_contract ON trellis.runs
    FOR EACH ROW EXECUTE FUNCTION trellis_private.protect_recovery_contract();

-- A transaction-local value attaches the contract at the original INSERT. An
-- idempotent acceptance retry cannot upgrade a legacy or existing execution.
CREATE FUNCTION trellis_private.attach_recovery_contract() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_value text:=current_setting('trellis.recovery_contract',true);
BEGIN
    IF v_value IS NOT NULL AND v_value<>'' THEN
        NEW.recovery_contract:=v_value::jsonb;
        PERFORM trellis_private.validate_recovery_contract(NEW.recovery_contract,NEW.client_request_id);
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.attach_recovery_contract()
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
CREATE TRIGGER attach_recovery_contract BEFORE INSERT ON trellis.runs
    FOR EACH ROW EXECUTE FUNCTION trellis_private.attach_recovery_contract();

CREATE FUNCTION trellis_private.set_model_call_purpose() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    NEW.purpose:=COALESCE(NEW.request_snapshot->>'purpose','generation');
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.set_model_call_purpose()
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
CREATE TRIGGER model_call_purpose BEFORE INSERT ON trellis.model_calls
    FOR EACH ROW EXECUTE FUNCTION trellis_private.set_model_call_purpose();

CREATE FUNCTION trellis_private.read_recovery_snapshot(p_run_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path='' AS $$
DECLARE v_result jsonb;
BEGIN
    WITH execution AS MATERIALIZED (
        SELECT * FROM trellis.runs WHERE user_id=(SELECT auth.uid()) AND id=p_run_id
    ), checkpoints AS MATERIALIZED (
        SELECT c.* FROM trellis.context_checkpoints c JOIN execution r ON c.chat_id=r.chat_id
        WHERE c.user_id=(SELECT auth.uid()) AND (c.run_id=r.id OR c.revision=(
            SELECT max(revision) FROM trellis.context_checkpoints WHERE user_id=c.user_id AND chat_id=c.chat_id))
        ORDER BY c.revision LIMIT 17
    ), calls AS MATERIALIZED (
        SELECT c.* FROM trellis.model_calls c JOIN execution r ON c.run_id=r.id
        WHERE c.user_id=(SELECT auth.uid()) ORDER BY step_index LIMIT 16
    ), tools AS MATERIALIZED (
        SELECT t.* FROM trellis.tool_calls t JOIN execution r ON t.run_id=r.id
        WHERE t.user_id=(SELECT auth.uid()) ORDER BY t.created_at,t.id LIMIT 31
    ), exchanges AS MATERIALIZED (
        SELECT m.* FROM trellis.run_messages m JOIN execution r ON m.run_id=r.id
        WHERE m.user_id=(SELECT auth.uid()) ORDER BY ordinal LIMIT 46
    ), outputs AS MATERIALIZED (
        SELECT o.* FROM trellis.run_assistant_outputs o JOIN execution r ON o.run_id=r.id
        WHERE o.user_id=(SELECT auth.uid()) ORDER BY step_index LIMIT 16
    ), transcript AS MATERIALIZED (
        SELECT m.* FROM trellis.messages m JOIN execution r ON m.chat_id=r.chat_id
        WHERE m.user_id=(SELECT auth.uid()) AND m.ordinal>COALESCE((
            SELECT covered_message_ordinal FROM checkpoints ORDER BY revision DESC LIMIT 1),0)
        ORDER BY ordinal LIMIT 129
    ) SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM execution) THEN NULL ELSE jsonb_build_object(
        'run',(SELECT to_jsonb(r)-'lease_token'-'request_fingerprint'-'event_storage_count'-'event_storage_bytes' FROM execution r),
        'model_calls',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY step_index) FROM calls c),'[]'::jsonb),
        'tool_calls',COALESCE((SELECT jsonb_agg(to_jsonb(t) ORDER BY created_at,id) FROM tools t),'[]'::jsonb),
        'run_messages',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY ordinal) FROM exchanges m),'[]'::jsonb),
        'outputs',COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY step_index) FROM outputs o),'[]'::jsonb),
        'messages',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY ordinal) FROM transcript m),'[]'::jsonb),
        'context_checkpoints',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY revision) FROM checkpoints c),'[]'::jsonb),
        'context_revision',(SELECT c.context_checkpoint_revision FROM trellis.chats c JOIN execution r ON c.id=r.chat_id)) END
    INTO v_result;
    IF v_result IS NOT NULL AND (octet_length(v_result::text)>16777216
       OR jsonb_array_length(v_result->'model_calls')>15 OR jsonb_array_length(v_result->'tool_calls')>30
       OR jsonb_array_length(v_result->'run_messages')>45 OR jsonb_array_length(v_result->'outputs')>15
       OR jsonb_array_length(v_result->'messages')>128 OR jsonb_array_length(v_result->'context_checkpoints')>16) THEN
        RAISE EXCEPTION 'recovery_snapshot_limit' USING ERRCODE='22023';
    END IF;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.read_recovery_snapshot(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.read_recovery_snapshot(uuid) TO authenticated;
CREATE FUNCTION trellis.read_recovery_snapshot(p_run_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
    SELECT trellis_private.read_recovery_snapshot(p_run_id)
$$;
REVOKE ALL ON FUNCTION trellis.read_recovery_snapshot(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis.read_recovery_snapshot(uuid) TO authenticated;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_recovery_contracts;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
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
            UPDATE trellis.tool_calls SET status='outcome_unknown',finished_at=v_now,
                result_source='unresolved_on_recovery_stop'
            WHERE user_id=v_user AND run_id=v_run.id AND status='running';
            SELECT id INTO v_uncertain FROM trellis.tool_calls
            WHERE user_id=v_user AND run_id=v_run.id AND status='outcome_unknown'
                AND COALESCE(recovery_policy,CASE WHEN name IN ('list_files','search_files','read_file','inspect_git')
                    THEN 'read_only' ELSE 'never' END)<>'read_only'
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
REVOKE ALL ON FUNCTION trellis_private.mutate_before_recovery_contracts(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
