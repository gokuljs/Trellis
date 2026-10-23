BEGIN;

ALTER TABLE trellis.tool_calls
    ADD COLUMN result_snapshot jsonb CHECK (jsonb_typeof(result_snapshot)='object' AND octet_length(result_snapshot::text)<=1048576),
    ADD COLUMN retry_generation integer CHECK (retry_generation>=0),
    ADD COLUMN retry_fingerprint text CHECK (retry_fingerprint ~ '^[0-9a-f]{64}$'),
    ADD COLUMN retry_owner_digest text CHECK (retry_owner_digest ~ '^[0-9a-f]{64}$'),
    ADD COLUMN retry_proof jsonb CHECK (jsonb_typeof(retry_proof)='object' AND octet_length(retry_proof::text)<=262144),
    ADD COLUMN retry_receipt jsonb CHECK (jsonb_typeof(retry_receipt)='object' AND octet_length(retry_receipt::text)<=16777216);
GRANT SELECT (result_snapshot) ON trellis.tool_calls TO authenticated;

CREATE OR REPLACE FUNCTION trellis_private.public_tool_call(p_row jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT p_row-'dispatch_arguments_hash'-'dispatch_fingerprint'-'dispatch_owner_digest'-'dispatch_receipt'
        -'result_fingerprint'-'result_owner_digest'-'result_receipt'
        -'retry_generation'-'retry_fingerprint'-'retry_owner_digest'-'retry_proof'-'retry_receipt'
$$;
REVOKE ALL ON FUNCTION trellis_private.public_tool_call(jsonb) FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

CREATE OR REPLACE FUNCTION trellis_private.preserve_uncertain_tools() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_uncertain uuid;
BEGIN
    UPDATE trellis.tool_calls SET status='outcome_unknown',finished_at=COALESCE(finished_at,NEW.finished_at,now()),
        result_source=COALESCE(result_source,'run_terminated_after_dispatch')
    WHERE user_id=NEW.user_id AND run_id=NEW.id
        AND (status='running' OR (status='pending' AND dispatch_attempt>0 AND retry_generation IS NOT NULL))
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

CREATE FUNCTION trellis_private.validate_patch_reconciliation(p_tool trellis.tool_calls,p_proof jsonb,p_observed text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF p_tool.name<>'apply_patch' OR p_tool.recovery_policy IS DISTINCT FROM 'patch'
       OR p_tool.approval_decision IS DISTINCT FROM 'approved'
       OR jsonb_typeof(p_tool.approval_preview) IS DISTINCT FROM 'object'
       OR NOT p_tool.approval_preview ?& ARRAY['target_sha256','desired_sha256']
       OR jsonb_typeof(p_tool.approval_preview->'desired_sha256') IS DISTINCT FROM 'string'
       OR p_tool.approval_preview->>'desired_sha256' !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_proof) IS DISTINCT FROM 'object' OR octet_length(p_proof::text)>262144
       OR NOT p_proof ?& ARRAY['before_sha256','after_sha256','observed_sha256']
       OR p_proof->'before_sha256' IS DISTINCT FROM p_tool.approval_preview->'target_sha256'
       OR p_proof->'after_sha256' IS DISTINCT FROM p_tool.approval_preview->'desired_sha256'
       OR p_proof->'observed_sha256' IS DISTINCT FROM p_proof->p_observed
       OR (p_tool.approval_preview->'target_sha256'<>'null'::jsonb AND (
            jsonb_typeof(p_tool.approval_preview->'target_sha256') IS DISTINCT FROM 'string'
            OR p_tool.approval_preview->>'target_sha256' !~ '^[0-9a-f]{64}$'))
       OR p_tool.input_snapshot->'approval_preview' IS DISTINCT FROM p_tool.approval_preview
       OR p_tool.input_snapshot->'arguments' IS DISTINCT FROM p_tool.arguments THEN
        RAISE EXCEPTION 'patch reconciliation differs from the approved operation' USING ERRCODE='22023';
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.validate_patch_reconciliation(trellis.tool_calls,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_local_reconciliation;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
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
        IF v_snapshot IS NOT NULL AND (jsonb_typeof(v_snapshot)<>'object' OR octet_length(v_snapshot::text)>1048576) THEN
            RAISE EXCEPTION 'invalid tool result snapshot' USING ERRCODE='22023';
        END IF;
        IF v_tool.status='pending' AND v_tool.dispatch_attempt>0
           AND v_tool.recovery_policy IS DISTINCT FROM 'read_only'
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
REVOKE ALL ON FUNCTION trellis_private.mutate_before_local_reconciliation(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
