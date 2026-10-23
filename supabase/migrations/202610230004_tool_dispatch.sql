BEGIN;

ALTER TABLE trellis.tool_calls
    DROP CONSTRAINT tool_calls_dispatch_attempt_check,
    ADD CONSTRAINT tool_calls_dispatch_attempt_check CHECK (dispatch_attempt BETWEEN 0 AND 4),
    ADD COLUMN dispatch_id uuid,
    ADD COLUMN input_snapshot jsonb CHECK (jsonb_typeof(input_snapshot)='object' AND octet_length(input_snapshot::text)<=262144),
    ADD COLUMN reviewed_at timestamptz,
    ADD COLUMN dispatch_arguments_hash text CHECK (dispatch_arguments_hash ~ '^[0-9a-f]{64}$'),
    ADD COLUMN dispatch_fingerprint text CHECK (dispatch_fingerprint ~ '^[0-9a-f]{64}$'),
    ADD COLUMN dispatch_owner_digest text CHECK (dispatch_owner_digest ~ '^[0-9a-f]{64}$'),
    ADD COLUMN dispatch_receipt jsonb CHECK (jsonb_typeof(dispatch_receipt)='object' AND octet_length(dispatch_receipt::text)<=16777216),
    ADD COLUMN result_fingerprint text CHECK (result_fingerprint ~ '^[0-9a-f]{64}$'),
    ADD COLUMN result_owner_digest text CHECK (result_owner_digest ~ '^[0-9a-f]{64}$'),
    ADD COLUMN result_receipt jsonb CHECK (jsonb_typeof(result_receipt)='object' AND octet_length(result_receipt::text)<=16777216);
CREATE UNIQUE INDEX tool_dispatch_identity ON trellis.tool_calls(user_id,dispatch_id) WHERE dispatch_id IS NOT NULL;
REVOKE SELECT ON trellis.tool_calls FROM authenticated;
GRANT SELECT (id,user_id,run_id,assistant_message_id,call_index,provider_call_id,name,arguments,status,
    approval_decision,approval_decided_at,approval_preview,finished_at,created_at,updated_at,
    dispatch_attempt,dispatch_generation,dispatched_at,recovery_policy,result_source,dispatch_id,input_snapshot,reviewed_at)
    ON trellis.tool_calls TO authenticated;

CREATE FUNCTION trellis_private.public_tool_call(p_row jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT p_row-'dispatch_arguments_hash'-'dispatch_fingerprint'-'dispatch_owner_digest'-'dispatch_receipt'
        -'result_fingerprint'-'result_owner_digest'-'result_receipt'
$$;
REVOKE ALL ON FUNCTION trellis_private.public_tool_call(jsonb) FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

CREATE FUNCTION trellis_private.preserve_uncertain_tools() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_uncertain uuid;
BEGIN
    UPDATE trellis.tool_calls SET status='outcome_unknown',finished_at=COALESCE(finished_at,NEW.finished_at,now()),
        result_source=COALESCE(result_source,'run_terminated_after_dispatch')
    WHERE user_id=NEW.user_id AND run_id=NEW.id AND status='running'
        AND COALESCE(recovery_policy,CASE WHEN name IN ('list_files','search_files','read_file','inspect_git')
            THEN 'read_only' ELSE 'never' END)<>'read_only';
    SELECT id INTO v_uncertain FROM trellis.tool_calls WHERE user_id=NEW.user_id AND run_id=NEW.id
        AND status='outcome_unknown' AND COALESCE(recovery_policy,CASE
            WHEN name IN ('list_files','search_files','read_file','inspect_git') THEN 'read_only' ELSE 'never' END)<>'read_only'
        ORDER BY created_at,id LIMIT 1;
    IF v_uncertain IS NOT NULL THEN
        NEW.retry_allowed:=false;
        NEW.uncertain_tool_call_id:=v_uncertain;
        NEW.recovery_reason:=COALESCE(NEW.recovery_reason,'tool_outcome_unknown');
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.preserve_uncertain_tools() FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
CREATE TRIGGER preserve_uncertain_tools BEFORE UPDATE OF status ON trellis.runs
FOR EACH ROW WHEN (NEW.status IN ('completed','failed','cancelled','interrupted'))
EXECUTE FUNCTION trellis_private.preserve_uncertain_tools();

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
    ), transcript AS MATERIALIZED (
        SELECT m.* FROM trellis.messages m JOIN execution r ON m.chat_id=r.chat_id
        WHERE m.user_id=(SELECT auth.uid()) AND m.ordinal>COALESCE((
            SELECT covered_message_ordinal FROM checkpoints ORDER BY revision DESC LIMIT 1),0)
        ORDER BY ordinal LIMIT 129
    ) SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM execution) THEN NULL ELSE jsonb_build_object(
        'run',(SELECT to_jsonb(r)-'lease_token'-'request_fingerprint'-'event_storage_count'-'event_storage_bytes' FROM execution r),
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
       OR jsonb_array_length(v_result->'messages')>128 OR jsonb_array_length(v_result->'context_checkpoints')>16) THEN
        RAISE EXCEPTION 'recovery_snapshot_limit' USING ERRCODE='22023';
    END IF;
    RETURN v_result;
END;
$$;
ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_tool_dispatch;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
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
        UPDATE trellis.tool_calls SET status='outcome_unknown',finished_at=v_now,result_source=p_payload->>'reason'
            WHERE id=v_tool.id AND user_id=v_user RETURNING * INTO v_tool;
        IF v_policy<>'read_only' THEN
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
REVOKE ALL ON FUNCTION trellis_private.mutate_before_tool_dispatch(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
