BEGIN;

ALTER TABLE trellis.runs
    ADD COLUMN recovery_stop_fingerprint text CHECK (recovery_stop_fingerprint ~ '^[0-9a-f]{64}$'),
    ADD COLUMN recovery_stop_owner_digest text CHECK (recovery_stop_owner_digest ~ '^[0-9a-f]{64}$'),
    ADD COLUMN recovery_stop_receipt jsonb CHECK (jsonb_typeof(recovery_stop_receipt)='object'
        AND octet_length(recovery_stop_receipt::text)<=1048576);

CREATE FUNCTION trellis_private.public_run(p_row jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT p_row-'lease_token'-'request_fingerprint'-'event_storage_count'-'event_storage_bytes'
        -'recovery_stop_fingerprint'-'recovery_stop_owner_digest'-'recovery_stop_receipt'
$$;
REVOKE ALL ON FUNCTION trellis_private.public_run(jsonb) FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

CREATE FUNCTION trellis_private.preserve_unknown_model_usage() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    UPDATE trellis.model_calls SET status='failed',accounted_total_tokens=NULL,estimated_cost=NULL,
        error_code='model_outcome_unknown',
        error_message='The dispatched model response was not saved; its final usage is unknown.',
        finished_at=COALESCE(finished_at,NEW.finished_at,clock_timestamp())
    WHERE user_id=NEW.user_id AND run_id=NEW.id AND status='streaming';
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.preserve_unknown_model_usage()
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
CREATE TRIGGER preserve_unknown_model_usage BEFORE UPDATE OF status ON trellis.runs
FOR EACH ROW WHEN (NEW.status IN ('completed','failed','cancelled','interrupted'))
EXECUTE FUNCTION trellis_private.preserve_unknown_model_usage();
UPDATE trellis.model_calls SET accounted_total_tokens=NULL,estimated_cost=NULL WHERE error_code='model_outcome_unknown';

CREATE OR REPLACE FUNCTION trellis_private.read_recovery_snapshot(p_run_id uuid) RETURNS jsonb
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
    ), transcript_head AS MATERIALIZED (
        SELECT m.* FROM trellis.messages m JOIN execution r ON m.chat_id=r.chat_id
        WHERE m.user_id=(SELECT auth.uid()) AND m.ordinal>COALESCE((
            SELECT covered_message_ordinal FROM checkpoints ORDER BY revision DESC LIMIT 1),0)
        ORDER BY ordinal LIMIT 128
    ), transcript AS MATERIALIZED (
        SELECT * FROM transcript_head UNION ALL
        SELECT m.* FROM trellis.messages m JOIN execution r ON m.id=r.input_message_id
            WHERE m.user_id=(SELECT auth.uid()) AND NOT EXISTS(SELECT 1 FROM transcript_head h WHERE h.id=m.id)
    ) SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM execution) THEN NULL ELSE jsonb_build_object(
        'run',(SELECT trellis_private.public_run(to_jsonb(r)) FROM execution r),
        'model_calls',COALESCE((SELECT jsonb_agg(to_jsonb(c)-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt' ORDER BY step_index) FROM calls c),'[]'::jsonb),
        'tool_calls',COALESCE((SELECT jsonb_agg(trellis_private.public_tool_call(to_jsonb(t)) ORDER BY created_at,id) FROM tools t),'[]'::jsonb),
        'run_messages',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY ordinal) FROM exchanges m),'[]'::jsonb),
        'outputs',COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY step_index) FROM outputs o),'[]'::jsonb),
        'messages',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY ordinal) FROM transcript m),'[]'::jsonb),
        'context_checkpoints',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY revision) FROM checkpoints c),'[]'::jsonb),
        'context_revision',(SELECT c.context_checkpoint_revision FROM trellis.chats c JOIN execution r ON c.id=r.chat_id)) END
    INTO v_result;
    IF v_result IS NOT NULL AND (octet_length(v_result::text)>16777216
       OR jsonb_array_length(v_result->'model_calls')>15 OR jsonb_array_length(v_result->'tool_calls')>30
       OR jsonb_array_length(v_result->'run_messages')>45 OR jsonb_array_length(v_result->'outputs')>15
       OR jsonb_array_length(v_result->'messages')>129 OR jsonb_array_length(v_result->'context_checkpoints')>16) THEN
        RAISE EXCEPTION 'recovery_snapshot_limit' USING ERRCODE='22023';
    END IF;
    RETURN v_result;
END;
$$;

CREATE OR REPLACE FUNCTION trellis.read_session_run_summaries(p_chat_id uuid,p_offset integer DEFAULT 0,
    p_limit integer DEFAULT 100,p_turn_id text DEFAULT NULL,p_newest_first boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE v_result jsonb;
BEGIN
    IF p_offset IS NULL OR p_offset<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 101
        OR p_newest_first IS NULL
        OR p_turn_id IS NOT NULL AND length(p_turn_id) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'invalid run summary page' USING ERRCODE='22023';
    END IF;
    WITH page AS MATERIALIZED (
        SELECT id,user_id,chat_id,turn_id,status,provider_id,model_id,adapter_kind,
            upstream_model_id,input_message_id,retry_of,client_request_id,
            max_model_calls,max_tool_calls,budget_preset,max_total_tokens,max_cost_usd,
            deadline_at,cancel_requested_at,lease_expires_at,recovery_count,stop_reason,
            last_event_sequence,error_code,error_message,created_at,started_at,finished_at,
            waiting_tool_call_id,generation,retry_allowed,recovery_reason,uncertain_tool_call_id
        FROM trellis.runs WHERE user_id=(SELECT auth.uid()) AND chat_id=p_chat_id
            AND (p_turn_id IS NULL OR turn_id=p_turn_id)
        ORDER BY CASE WHEN p_newest_first THEN created_at END DESC,
            CASE WHEN p_newest_first THEN id END DESC,
            CASE WHEN NOT p_newest_first THEN created_at END,
            CASE WHEN NOT p_newest_first THEN id END
        OFFSET p_offset LIMIT p_limit
    ), model_totals AS (
        SELECT c.run_id,count(*) AS model_call_count,
            sum(c.accounted_total_tokens) AS total_tokens,
            bool_and(c.accounted_total_tokens IS NOT NULL
                AND c.status NOT IN ('pending','streaming') AND c.error_code IS DISTINCT FROM 'model_outcome_unknown') AS usage_complete,
            sum(c.estimated_cost) AS estimated_cost_usd,
            bool_and(c.estimated_cost IS NOT NULL
                AND c.status NOT IN ('pending','streaming') AND c.error_code IS DISTINCT FROM 'model_outcome_unknown') AS cost_complete
        FROM trellis.model_calls c JOIN page p ON c.run_id=p.id AND c.user_id=p.user_id
        WHERE c.user_id=(SELECT auth.uid()) GROUP BY c.run_id
    ), tool_totals AS (
        SELECT c.run_id,count(*) AS tool_call_count
        FROM trellis.tool_calls c JOIN page p ON c.run_id=p.id AND c.user_id=p.user_id
        WHERE c.user_id=(SELECT auth.uid()) GROUP BY c.run_id
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'run',to_jsonb(p),
        'model_call_count',COALESCE(m.model_call_count,0),
        'tool_call_count',COALESCE(t.tool_call_count,0),
        'total_tokens',CASE WHEN m.model_call_count IS NULL THEN 0 ELSE m.total_tokens END,
        'usage_complete',COALESCE(m.usage_complete,true),
        'estimated_cost_usd',CASE WHEN m.model_call_count IS NULL THEN 0 ELSE m.estimated_cost_usd END,
        'cost_complete',COALESCE(m.cost_complete,true))
        ORDER BY CASE WHEN p_newest_first THEN p.created_at END DESC,
            CASE WHEN p_newest_first THEN p.id END DESC,
            CASE WHEN NOT p_newest_first THEN p.created_at END,
            CASE WHEN NOT p_newest_first THEN p.id END),'[]'::jsonb)
    INTO v_result FROM page p LEFT JOIN model_totals m ON m.run_id=p.id
        LEFT JOIN tool_totals t ON t.run_id=p.id;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis.read_session_run_summaries(uuid,integer,integer,text,boolean)
    FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.read_session_run_summaries(uuid,integer,integer,text,boolean)
    TO authenticated;


ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_execution_recovery;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE; v_call trellis.model_calls%ROWTYPE;
    v_tool trellis.tool_calls%ROWTYPE; v_result jsonb; v_event jsonb; v_fingerprint text; v_digest text;
    v_cancel boolean; v_usage_unknown boolean; v_reason text; v_now timestamptz:=clock_timestamp();
BEGIN
    IF p_action='stop_recovery' THEN
        IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object'
           OR COALESCE(length(p_payload->>'reason'),0) NOT BETWEEN 1 AND 100
           OR jsonb_typeof(p_payload->'retry_allowed') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_payload->'usage_unknown') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'invalid recovery stop' USING ERRCODE='22023';
        END IF;
        v_digest:=trellis_private.capability_digest(p_capability);
        SELECT * INTO v_run FROM trellis.runs WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
        v_fingerprint:=encode(sha256(convert_to(jsonb_build_object('action',p_action,'payload',p_payload,
            'generation',v_run.generation)::text,'UTF8')),'hex');
        IF v_run.recovery_stop_receipt IS NOT NULL THEN
            IF v_run.recovery_stop_owner_digest IS DISTINCT FROM v_digest THEN
                RAISE EXCEPTION 'recovery stop receipt ownership invalid' USING ERRCODE='42501';
            END IF;
            IF v_run.recovery_stop_fingerprint IS DISTINCT FROM v_fingerprint THEN
                RAISE EXCEPTION 'recovery stop conflicts with saved content' USING ERRCODE='22023';
            END IF;
            RETURN v_run.recovery_stop_receipt;
        END IF;
        v_run:=trellis_private.require_run(v_run.id,p_capability);
        IF v_run.status NOT IN ('recovering','running','cancelling') THEN
            RAISE EXCEPTION 'run is not in recoverable execution' USING ERRCODE='22023';
        END IF;
        IF p_payload->>'model_call_id' IS NOT NULL THEN
            SELECT * INTO v_call FROM trellis.model_calls WHERE id=(p_payload->>'model_call_id')::uuid
                AND run_id=v_run.id AND user_id=v_user;
            IF NOT FOUND THEN RAISE EXCEPTION 'recovery model call not found' USING ERRCODE='22023'; END IF;
            IF (p_payload->>'usage_unknown')::boolean AND v_call.status='completed' THEN
                RAISE EXCEPTION 'saved model completion cannot have an unknown outcome' USING ERRCODE='22023';
            END IF;
        END IF;
        IF p_payload->>'tool_call_id' IS NOT NULL THEN
            SELECT * INTO v_tool FROM trellis.tool_calls WHERE id=(p_payload->>'tool_call_id')::uuid
                AND run_id=v_run.id AND user_id=v_user;
            IF NOT FOUND THEN RAISE EXCEPTION 'recovery tool call not found' USING ERRCODE='22023'; END IF;
        END IF;
        IF (p_payload->>'usage_unknown')::boolean THEN
            UPDATE trellis.model_calls SET status='failed',accounted_total_tokens=NULL,estimated_cost=NULL,
                error_code='model_outcome_unknown',
                error_message='The dispatched model response was not saved; its final usage is unknown.',finished_at=v_now
            WHERE user_id=v_user AND run_id=v_run.id AND (status='streaming'
                OR (id=(p_payload->>'model_call_id')::uuid AND status IN ('failed','cancelled','timed_out')));
        END IF;
        v_usage_unknown:=(p_payload->>'usage_unknown')::boolean OR EXISTS(
            SELECT 1 FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id
                AND (status='streaming' OR error_code='model_outcome_unknown'));
        v_cancel:=v_run.cancel_requested_at IS NOT NULL OR v_run.status='cancelling';
        v_reason:=CASE WHEN v_cancel THEN 'user_cancelled' ELSE p_payload->>'reason' END;
        UPDATE trellis.runs SET status=CASE WHEN v_cancel THEN 'cancelled' ELSE 'interrupted' END,
            finished_at=v_now,waiting_tool_call_id=NULL,lease_token=NULL,lease_expires_at=NULL,
            retry_allowed=retry_allowed AND (p_payload->>'retry_allowed')::boolean,
            recovery_reason=v_reason,stop_reason=v_reason,error_code=v_reason,
            error_message=CASE WHEN v_cancel THEN 'The run was cancelled.'
                WHEN v_usage_unknown THEN 'The dispatched model response was lost. Partial output is saved; final usage is unknown.'
                ELSE CASE v_reason
                    WHEN 'incompatible_contract' THEN 'The saved run uses an unsupported recovery contract.'
                    WHEN 'incompatible_runtime' THEN 'The saved run requires a different runtime version.'
                    WHEN 'incompatible_adapter' THEN 'The saved model adapter version is no longer compatible.'
                    WHEN 'incompatible_tool' THEN 'The saved tool version is no longer compatible.'
                    WHEN 'different_installation' THEN 'The saved run belongs to another installation on a different computer.'
                    WHEN 'workspace_unavailable' THEN 'The trusted folder is unavailable. Select the original folder again.'
                    WHEN 'workspace_grant_unavailable' THEN 'The trusted folder permission is unavailable. Select the folder again.'
                    WHEN 'workspace_changed' THEN 'The saved folder identity or permission changed. Select the original folder again.'
                    WHEN 'inconsistent_execution_records' THEN 'The saved execution records do not form a consistent run.'
                    WHEN 'request_identity_changed' THEN 'The saved request identity no longer matches the original run.'
                    WHEN 'deadline_exceeded' THEN 'The original run deadline expired.'
                    WHEN 'recovery_limit_exceeded' THEN 'The run reached its recovery takeover limit.'
                    WHEN 'tool_outcome_unknown' THEN 'A dispatched tool has no confirmed result. Review its effects before starting more work.'
                    WHEN 'read_retry_exhausted' THEN 'The interrupted read exhausted its one recovery retry.'
                    WHEN 'patch_conflict' THEN 'The file matches neither its approved original nor its desired contents. Review the file before continuing.'
                    WHEN 'legacy_run' THEN 'This run was created before checkpoint recovery was available.'
                    ELSE 'The saved run could not be safely continued: '||replace(v_reason,'_',' ')||'.' END END
            WHERE id=v_run.id AND user_id=v_user RETURNING * INTO v_run;
        v_event:=trellis_private.append_event(v_user,v_run.id,CASE WHEN v_cancel THEN 'run.cancelled' ELSE 'run.interrupted' END,
            jsonb_build_object('status',v_run.status,'code',v_reason,'message',v_run.error_message,
                'usage_unknown',v_usage_unknown,
                'model_call_id',p_payload->>'model_call_id','tool_call_id',p_payload->>'tool_call_id'));
        SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id AND user_id=v_user;
        v_result:=jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'events',jsonb_build_array(v_event));
        UPDATE trellis.runs SET recovery_stop_fingerprint=v_fingerprint,recovery_stop_owner_digest=v_digest,
            recovery_stop_receipt=v_result WHERE id=v_run.id AND user_id=v_user;
        RETURN v_result;
    ELSIF p_action='create_model_call' THEN
        v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        IF v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL OR v_run.waiting_tool_call_id IS NOT NULL THEN
            RAISE EXCEPTION 'run not ready for model call' USING ERRCODE='22023';
        END IF;
        SELECT * INTO v_call FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id
            AND step_index=(p_payload->>'step_index')::integer FOR UPDATE;
        IF FOUND THEN
            IF v_call.model_id IS DISTINCT FROM p_payload->>'model_id'
               OR v_call.request_snapshot IS DISTINCT FROM p_payload->'request_snapshot' THEN
                RAISE EXCEPTION 'model call step conflicts with saved request' USING ERRCODE='22023';
            END IF;
            RETURN to_jsonb(v_call)-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt';
        END IF;
    ELSIF p_action='update_model_call' AND p_payload->>'next_status'='streaming' THEN
        SELECT * INTO v_call FROM trellis.model_calls WHERE id=(p_payload->>'call_id')::uuid AND user_id=v_user;
        IF NOT FOUND THEN RAISE EXCEPTION 'model call not found' USING ERRCODE='22023'; END IF;
        v_run:=trellis_private.require_run(v_call.run_id,p_capability);
        IF v_call.status='pending' AND (v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL
            OR v_run.waiting_tool_call_id IS NOT NULL) THEN
            RAISE EXCEPTION 'run not ready for model dispatch' USING ERRCODE='22023';
        END IF;
    ELSIF p_action='relinquish_run' THEN
        SELECT * INTO v_run FROM trellis.runs WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user FOR UPDATE;
        IF FOUND AND v_run.recovery_contract IS NOT NULL
           AND v_run.status IN ('queued','running','recovering','waiting_for_approval','cancelling')
           AND v_run.lease_expires_at<=v_now AND v_run.lease_token=trellis_private.capability_digest(p_capability) THEN
            RETURN trellis_private.public_run(to_jsonb(v_run));
        END IF;
    ELSIF p_action='finish_recovery' THEN
        v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        IF v_run.status IN ('running','waiting_for_approval','cancelling') THEN
            SELECT to_jsonb(e) INTO v_event FROM trellis.run_events e WHERE user_id=v_user AND run_id=v_run.id
                AND event_type='run.resumed' AND data->>'generation'=v_run.generation::text
                ORDER BY sequence DESC LIMIT 1;
            IF v_event IS NOT NULL THEN
                RETURN jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'event',v_event);
            END IF;
        END IF;
    END IF;
    v_result:=trellis_private.mutate_before_execution_recovery(p_action,p_payload,p_capability);
    IF p_action='finish_recovery' AND jsonb_typeof(v_result->'run')='object' THEN
        SELECT * INTO v_run FROM trellis.runs WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user;
        v_result:=jsonb_set(v_result,'{run}',trellis_private.public_run(to_jsonb(v_run)));
    END IF;
    IF jsonb_typeof(v_result)='object' THEN
        IF jsonb_typeof(v_result->'run')='object' THEN v_result:=jsonb_set(v_result,'{run}',trellis_private.public_run(v_result->'run')); END IF;
        IF v_result ? 'recovery_stop_receipt' THEN v_result:=trellis_private.public_run(v_result); END IF;
    ELSIF p_action='list_recovery_candidates' AND jsonb_typeof(v_result)='array' THEN
        SELECT COALESCE(jsonb_agg(trellis_private.public_run(r.value) ORDER BY r.ordinality),'[]'::jsonb) INTO v_result
            FROM jsonb_array_elements(v_result) WITH ORDINALITY r(value,ordinality);
    END IF;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_execution_recovery(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
