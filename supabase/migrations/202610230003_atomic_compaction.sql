BEGIN;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_atomic_compaction;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_call trellis.model_calls%ROWTYPE; v_native trellis.model_calls%ROWTYPE;
    v_completion jsonb:=p_payload->'completion'; v_draft jsonb:=p_payload->'draft';
    v_intent jsonb; v_native_intent jsonb; v_native_payload jsonb; v_fingerprint text; v_digest text;
    v_saved jsonb; v_checkpoint jsonb; v_event jsonb; v_result jsonb; v_key text;
BEGIN
    IF p_action NOT IN ('commit_summary','commit_native_compaction') THEN
        RETURN trellis_private.mutate_before_atomic_compaction(p_action,p_payload,p_capability);
    END IF;
    IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object'
       OR jsonb_typeof(v_completion) IS DISTINCT FROM 'object'
       OR (p_action='commit_summary' AND jsonb_typeof(v_draft) IS DISTINCT FROM 'object')
       OR octet_length(p_payload::text)>8388608 THEN
        RAISE EXCEPTION 'invalid summary commit' USING ERRCODE='22023';
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
            RAISE EXCEPTION 'summary receipt ownership invalid' USING ERRCODE='42501';
        END IF;
        IF v_call.completion_fingerprint IS DISTINCT FROM v_fingerprint THEN
            RAISE EXCEPTION 'summary commit conflicts with saved content' USING ERRCODE='22023';
        END IF;
        RETURN v_call.completion_receipt;
    END IF;
    v_run:=trellis_private.require_run(v_run.id,p_capability);
    IF v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL
       OR v_run.waiting_tool_call_id IS NOT NULL OR v_call.status<>'streaming'
       OR v_call.purpose IS DISTINCT FROM (CASE WHEN p_action='commit_summary' THEN 'summary' ELSE 'native_compaction' END) THEN
        RAISE EXCEPTION 'run not ready for summary commit' USING ERRCODE='22023';
    END IF;
    FOR v_key IN SELECT jsonb_object_keys(v_completion) LOOP
        IF v_key NOT IN ('response_snapshot','provider_response_id','finish_reason','input_tokens','output_tokens',
            'reasoning_tokens','cached_read_tokens','cache_creation_tokens','estimated_cost','clear_estimated_cost',
            'accounted_total_tokens','clear_accounted_total_tokens')
           AND NOT (p_action='commit_native_compaction' AND v_key IN ('error_code','error_message')) THEN
            RAISE EXCEPTION 'invalid summary completion field' USING ERRCODE='22023';
        END IF;
    END LOOP;
    v_intent:=v_call.request_snapshot->'compaction_intent';
    IF p_action='commit_native_compaction' THEN
        IF p_payload->>'next_status' IS NULL OR p_payload->>'next_status' NOT IN ('completed','failed')
           OR jsonb_typeof(v_intent) IS DISTINCT FROM 'object' OR v_intent->'version' IS DISTINCT FROM '1'::jsonb
           OR v_intent->>'operation_id' IS NULL OR v_intent->>'target_checkpoint_id' IS NULL
           OR jsonb_typeof(v_intent->'sources') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_completion->'response_snapshot') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'invalid native compaction boundary' USING ERRCODE='22023';
        END IF;
        PERFORM (v_intent->>'operation_id')::uuid;
        PERFORM (v_intent->>'target_checkpoint_id')::uuid;
        IF p_payload->>'next_status'='completed' THEN
            IF v_completion->>'error_code' IS NOT NULL
               OR jsonb_typeof(v_completion#>'{response_snapshot,native_payload_json}') IS DISTINCT FROM 'string'
               OR octet_length(v_completion#>>'{response_snapshot,native_payload_json}')>262144 THEN
                RAISE EXCEPTION 'invalid native compaction result' USING ERRCODE='22023';
            END IF;
            v_native_payload:=(v_completion#>>'{response_snapshot,native_payload_json}')::jsonb;
            IF (CASE
                WHEN v_completion#>>'{response_snapshot,native_kind}'='openai_window' AND v_run.adapter_kind='openai'
                    THEN CASE WHEN jsonb_typeof(v_native_payload)='array' THEN jsonb_array_length(v_native_payload)>0 ELSE false END
                WHEN v_completion#>>'{response_snapshot,native_kind}'='anthropic_block' AND v_run.adapter_kind='anthropic'
                    THEN jsonb_typeof(v_native_payload)='object' AND v_native_payload->>'type'='compaction'
                ELSE false END) IS NOT TRUE THEN
                RAISE EXCEPTION 'native compaction result differs from run adapter' USING ERRCODE='22023';
            END IF;
        ELSIF v_completion->>'error_code' IS DISTINCT FROM 'native_context_invalid'
           OR v_completion#>>'{response_snapshot,native_payload_json}' IS NOT NULL THEN
            RAISE EXCEPTION 'native compaction failure is unresolved' USING ERRCODE='22023';
        END IF;
        v_saved:=trellis_private.mutate_before_atomic_compaction('update_model_call',v_completion||
            jsonb_build_object('call_id',v_call.id,'next_status',p_payload->>'next_status'),p_capability);
        IF v_saved->>'accounted_total_tokens' IS NULL OR v_saved->>'estimated_cost' IS NULL THEN
            RAISE EXCEPTION 'native compaction usage is unresolved' USING ERRCODE='22023';
        END IF;
        v_result:=jsonb_build_object('model_call',v_saved,'events','[]'::jsonb);
        IF p_payload->>'next_status'='completed' THEN
            v_event:=trellis_private.append_event(v_user,v_run.id,'model.completed',jsonb_build_object(
                'model_call_id',v_call.id,'purpose','native_compaction','operation_id',v_intent->>'operation_id'));
            v_result:=jsonb_set(v_result,'{events}',jsonb_build_array(v_event));
        END IF;
        UPDATE trellis.model_calls SET completion_fingerprint=v_fingerprint,completion_owner_digest=v_digest,
            completion_receipt=v_result WHERE id=v_call.id AND user_id=v_user;
        RETURN v_result;
    END IF;
    IF jsonb_typeof(v_intent) IS DISTINCT FROM 'object' OR v_intent->'version' IS DISTINCT FROM '1'::jsonb
       OR NOT v_intent ?& ARRAY['operation_id','target_checkpoint_id','expected_revision','parent_checkpoint_id',
            'sources','source_hash','input_hash','instruction_hash','workspace_hash','input_tokens_before',
            'pricing_context_tokens','native_model_call_id','prefix_length','generation_plan','summary_plan']
       OR jsonb_typeof(v_intent->'generation_plan') IS DISTINCT FROM 'object'
       OR jsonb_typeof(v_intent->'summary_plan') IS DISTINCT FROM 'object'
       OR v_intent->>'operation_id' IS NULL OR v_intent->>'target_checkpoint_id' IS NULL
       OR jsonb_typeof(v_intent->'expected_revision') IS DISTINCT FROM 'number'
       OR v_intent->>'target_checkpoint_id' IS DISTINCT FROM v_draft->>'id'
       OR v_draft->>'model_call_id' IS DISTINCT FROM v_call.id::text
       OR v_intent->'expected_revision' IS DISTINCT FROM p_payload->'expected_revision'
       OR v_intent->'sources' IS DISTINCT FROM v_draft->'sources'
       OR v_intent->>'source_hash' IS DISTINCT FROM v_draft->>'source_hash'
       OR v_intent->>'input_hash' IS DISTINCT FROM v_draft->>'input_hash'
       OR v_intent->>'instruction_hash' IS DISTINCT FROM v_draft->>'instruction_hash'
       OR v_intent->>'workspace_hash' IS DISTINCT FROM v_draft->>'workspace_hash'
       OR v_intent->'input_tokens_before' IS DISTINCT FROM v_draft->'input_tokens_before'
       OR jsonb_typeof(v_intent->'sources') IS DISTINCT FROM 'array'
       OR jsonb_array_length(v_intent->'sources') IS DISTINCT FROM (v_intent->>'prefix_length')::integer
       OR (v_draft->>'pricing_context_tokens')::integer<COALESCE((v_intent->>'pricing_context_tokens')::integer,0)
       OR jsonb_typeof(v_completion#>'{response_snapshot,content}') IS DISTINCT FROM 'string'
       OR v_completion#>>'{response_snapshot,content}' IS DISTINCT FROM v_draft->>'summary' THEN
        RAISE EXCEPTION 'summary checkpoint differs from prepared compaction intent' USING ERRCODE='22023';
    END IF;
    PERFORM (v_intent->>'operation_id')::uuid;
    PERFORM (v_intent->>'target_checkpoint_id')::uuid;
    IF v_intent->>'native_model_call_id' IS NOT NULL THEN
        SELECT * INTO v_native FROM trellis.model_calls WHERE id=(v_intent->>'native_model_call_id')::uuid
            AND user_id=v_user AND run_id=v_run.id AND purpose='native_compaction'
            AND step_index<v_call.step_index;
        IF NOT FOUND THEN RAISE EXCEPTION 'native compaction source not found' USING ERRCODE='22023'; END IF;
        v_native_intent:=v_native.request_snapshot->'compaction_intent';
        IF v_native_intent->>'operation_id' IS DISTINCT FROM v_intent->>'operation_id'
           OR v_native_intent->>'target_checkpoint_id' IS DISTINCT FROM v_intent->>'target_checkpoint_id'
           OR v_native_intent->'expected_revision' IS DISTINCT FROM v_intent->'expected_revision'
           OR v_native_intent->'parent_checkpoint_id' IS DISTINCT FROM v_intent->'parent_checkpoint_id'
           OR v_native_intent->'sources' IS DISTINCT FROM v_intent->'sources'
           OR v_native_intent->>'source_hash' IS DISTINCT FROM v_intent->>'source_hash'
           OR v_native_intent->>'input_hash' IS DISTINCT FROM v_intent->>'input_hash'
           OR v_native_intent->>'instruction_hash' IS DISTINCT FROM v_intent->>'instruction_hash'
           OR v_native_intent->>'workspace_hash' IS DISTINCT FROM v_intent->>'workspace_hash'
           OR v_native.accounted_total_tokens IS NULL OR v_native.estimated_cost IS NULL THEN
            RAISE EXCEPTION 'native compaction source differs from prepared operation' USING ERRCODE='22023';
        END IF;
        IF v_native.status='completed' THEN
            IF v_native.response_snapshot->>'native_kind' IS NULL
               OR v_native.response_snapshot->>'native_payload_json' IS NULL
               OR v_draft->>'native_kind' IS DISTINCT FROM v_native.response_snapshot->>'native_kind'
               OR v_draft->>'native_payload_json' IS DISTINCT FROM v_native.response_snapshot->>'native_payload_json' THEN
                RAISE EXCEPTION 'checkpoint differs from saved native compaction result' USING ERRCODE='22023';
            END IF;
        ELSIF v_native.status<>'failed' OR v_native.error_code IS DISTINCT FROM 'native_context_invalid'
           OR v_draft->>'native_kind' IS NOT NULL OR v_draft->>'native_payload_json' IS NOT NULL THEN
            RAISE EXCEPTION 'native compaction outcome is unresolved' USING ERRCODE='22023';
        END IF;
    ELSIF v_draft->>'native_kind' IS NOT NULL OR v_draft->>'native_payload_json' IS NOT NULL THEN
        RAISE EXCEPTION 'checkpoint has no original native compaction operation' USING ERRCODE='22023';
    END IF;
    v_saved:=trellis_private.mutate_before_atomic_compaction('update_model_call',v_completion||
        jsonb_build_object('call_id',v_call.id,'next_status','completed'),p_capability);
    IF v_saved->>'accounted_total_tokens' IS NULL OR v_saved->>'estimated_cost' IS NULL THEN
        RAISE EXCEPTION 'summary usage is unresolved' USING ERRCODE='22023';
    END IF;
    v_checkpoint:=trellis_private.mutate_before_atomic_compaction('save_context_checkpoint',v_draft||
        jsonb_build_object('run_id',v_run.id,'expected_revision',p_payload->'expected_revision'),p_capability);
    IF v_checkpoint->>'parent_id' IS DISTINCT FROM v_intent->>'parent_checkpoint_id' THEN
        RAISE EXCEPTION 'compaction checkpoint parent changed' USING ERRCODE='22023';
    END IF;
    v_event:=trellis_private.append_event(v_user,v_run.id,'model.completed',jsonb_build_object(
        'model_call_id',v_call.id,'purpose','summary','checkpoint_id',v_checkpoint->>'id',
        'operation_id',v_intent->>'operation_id'));
    v_result:=jsonb_build_object('model_call',v_saved,'checkpoint',v_checkpoint,'events',jsonb_build_array(v_event));
    UPDATE trellis.model_calls SET completion_fingerprint=v_fingerprint,completion_owner_digest=v_digest,
        completion_receipt=v_result WHERE id=v_call.id AND user_id=v_user;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_atomic_compaction(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
