BEGIN;

ALTER TABLE trellis.model_calls
    ADD COLUMN completion_fingerprint text CHECK (completion_fingerprint ~ '^[0-9a-f]{64}$'),
    ADD COLUMN completion_owner_digest text CHECK (completion_owner_digest ~ '^[0-9a-f]{64}$'),
    ADD COLUMN completion_receipt jsonb CHECK (
        jsonb_typeof(completion_receipt)='object' AND octet_length(completion_receipt::text)<=16777216),
    ADD CONSTRAINT model_completion_receipt_complete CHECK (
        (completion_fingerprint IS NULL AND completion_owner_digest IS NULL AND completion_receipt IS NULL)
        OR (completion_fingerprint IS NOT NULL AND completion_owner_digest IS NOT NULL AND completion_receipt IS NOT NULL));
REVOKE SELECT ON trellis.model_calls FROM authenticated;
GRANT SELECT (id,user_id,run_id,step_index,provider_id,model_id,adapter_kind,status,
    request_snapshot,response_snapshot,provider_response_id,finish_reason,input_tokens,output_tokens,
    reasoning_tokens,cached_read_tokens,cache_creation_tokens,estimated_cost,error_code,error_message,
    started_at,finished_at,created_at,updated_at,accounted_total_tokens,purpose)
    ON trellis.model_calls TO authenticated;

ALTER FUNCTION trellis_private.read_recovery_snapshot(uuid) RENAME TO read_recovery_snapshot_before_atomic_generation;
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
        'model_calls',COALESCE((SELECT jsonb_agg(to_jsonb(c)-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt' ORDER BY step_index) FROM calls c),'[]'::jsonb),
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
REVOKE ALL ON FUNCTION trellis_private.read_recovery_snapshot_before_atomic_generation(uuid)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.read_recovery_snapshot(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.read_recovery_snapshot(uuid) TO authenticated;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_atomic_generation;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_call trellis.model_calls%ROWTYPE; v_completion jsonb:=p_payload->'completion';
    v_assistant jsonb:=p_payload->'assistant_message'; v_final text:=p_payload->>'final_content';
    v_response jsonb; v_fingerprint text; v_digest text; v_result jsonb;
    v_saved jsonb; v_event jsonb; v_events jsonb; v_key text;
BEGIN
    IF p_action<>'commit_generation' THEN
        v_result:=trellis_private.mutate_before_atomic_generation(p_action,p_payload,p_capability);
        IF p_action IN ('create_model_call','update_model_call') THEN
            RETURN v_result-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt';
        END IF;
        RETURN v_result;
    END IF;
    IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object'
       OR jsonb_typeof(v_completion) IS DISTINCT FROM 'object'
       OR octet_length(p_payload::text)>8388608 THEN
        RAISE EXCEPTION 'invalid generation commit' USING ERRCODE='22023';
    END IF;
    v_fingerprint:=encode(sha256(convert_to(jsonb_build_object('action',p_action,'payload',p_payload)::text,'UTF8')),'hex');
    v_digest:=trellis_private.capability_digest(p_capability);
    SELECT * INTO v_run FROM trellis.runs
        WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='42501'; END IF;
    SELECT * INTO v_call FROM trellis.model_calls WHERE id=(p_payload->>'call_id')::uuid
        AND run_id=v_run.id AND user_id=v_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'model call not found' USING ERRCODE='22023'; END IF;
    IF v_call.completion_receipt IS NOT NULL THEN
        IF v_call.completion_owner_digest IS DISTINCT FROM v_digest THEN
            RAISE EXCEPTION 'generation receipt ownership invalid' USING ERRCODE='42501';
        END IF;
        IF v_call.completion_fingerprint IS DISTINCT FROM v_fingerprint THEN
            RAISE EXCEPTION 'generation commit conflicts with saved content' USING ERRCODE='22023';
        END IF;
        RETURN v_call.completion_receipt;
    END IF;
    v_run:=trellis_private.require_run(v_run.id,p_capability);
    IF v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL
       OR v_run.waiting_tool_call_id IS NOT NULL OR v_call.status<>'streaming' OR v_call.purpose<>'generation' THEN
        RAISE EXCEPTION 'run not ready for generation commit' USING ERRCODE='22023';
    END IF;
    FOR v_key IN SELECT jsonb_object_keys(v_completion) LOOP
        IF v_key NOT IN ('response_snapshot','provider_response_id','finish_reason','input_tokens','output_tokens',
            'reasoning_tokens','cached_read_tokens','cache_creation_tokens','estimated_cost','clear_estimated_cost',
            'accounted_total_tokens','clear_accounted_total_tokens') THEN
            RAISE EXCEPTION 'invalid generation completion field' USING ERRCODE='22023';
        END IF;
    END LOOP;
    v_response:=v_completion->'response_snapshot';
    IF jsonb_typeof(v_response) IS DISTINCT FROM 'object'
       OR jsonb_typeof(v_response->'tool_calls') IS DISTINCT FROM 'array'
       OR jsonb_typeof(v_response->'continuation_items') IS DISTINCT FROM 'array'
       OR jsonb_typeof(v_response->'content') IS DISTINCT FROM 'string'
       OR v_response->>'provider_response_id' IS DISTINCT FROM v_completion->>'provider_response_id'
       OR v_response->>'finish_reason' IS DISTINCT FROM v_completion->>'finish_reason' THEN
        RAISE EXCEPTION 'invalid generation response snapshot' USING ERRCODE='22023';
    END IF;
    IF v_final IS NOT NULL THEN
        IF jsonb_typeof(p_payload->'final_content') IS DISTINCT FROM 'string'
           OR v_assistant IS DISTINCT FROM 'null'::jsonb AND v_assistant IS NOT NULL
           OR btrim(v_final)='' OR v_final IS DISTINCT FROM v_response->>'content'
           OR jsonb_array_length(v_response->'tool_calls')<>0 OR v_response->>'finish_kind' IS DISTINCT FROM 'final' THEN
            RAISE EXCEPTION 'invalid final generation boundary' USING ERRCODE='22023';
        END IF;
    ELSE
        IF jsonb_typeof(v_assistant) IS DISTINCT FROM 'object'
           OR v_assistant->>'role' IS DISTINCT FROM 'assistant'
           OR v_assistant->>'content' IS DISTINCT FROM v_response->>'content'
           OR v_assistant->'tool_calls' IS DISTINCT FROM v_response->'tool_calls'
           OR v_assistant->'continuation_items' IS DISTINCT FROM v_response->'continuation_items'
           OR jsonb_array_length(v_response->'tool_calls')=0 OR v_response->>'finish_kind' IS DISTINCT FROM 'tool'
           OR v_call.step_index>=v_run.max_model_calls
           OR (SELECT count(*) FROM trellis.tool_calls WHERE run_id=v_run.id AND user_id=v_user)
                +jsonb_array_length(v_response->'tool_calls')>v_run.max_tool_calls THEN
            RAISE EXCEPTION 'invalid tool generation boundary' USING ERRCODE='22023';
        END IF;
    END IF;
    v_saved:=trellis_private.mutate_before_atomic_generation('update_model_call',v_completion||
        jsonb_build_object('call_id',v_call.id,'next_status','completed'),p_capability);
    v_event:=trellis_private.append_event(v_user,v_run.id,'model.completed',jsonb_build_object(
        'model_call_id',v_call.id,'provider_response_id',v_completion->>'provider_response_id',
        'finish_reason',v_completion->>'finish_reason','finish_kind',v_response->>'finish_kind'));
    v_events:=jsonb_build_array(v_event);
    IF v_final IS NOT NULL THEN
        v_result:=trellis_private.mutate_before_atomic_generation('complete_run',
            jsonb_build_object('run_id',v_run.id,'content',v_final),p_capability);
        UPDATE trellis.messages SET content=v_final
            WHERE id=(v_result#>>'{message,id}')::uuid AND user_id=v_user;
    ELSE
        PERFORM trellis_private.mutate_before_atomic_generation('finish_assistant_output',jsonb_build_object(
            'run_id',v_run.id,'model_call_id',v_call.id,'stop_reason','tool_continuation'),p_capability);
        v_result:=trellis_private.mutate_before_atomic_generation('record_assistant_message',jsonb_build_object(
            'run_id',v_run.id,'model_call_id',v_call.id,'message',v_assistant),p_capability);
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE id=v_run.id AND user_id=v_user;
    v_result:=jsonb_build_object('model_call',v_saved-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt',
        'tool_calls',COALESCE(v_result->'tool_calls','[]'::jsonb),
        'events',v_events||COALESCE(v_result->'events','[]'::jsonb),
        'run',to_jsonb(v_run)-'lease_token'-'request_fingerprint');
    UPDATE trellis.model_calls SET completion_fingerprint=v_fingerprint,completion_owner_digest=v_digest,
        completion_receipt=v_result WHERE id=v_call.id AND user_id=v_user;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_atomic_generation(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
