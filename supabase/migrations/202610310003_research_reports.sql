BEGIN;

CREATE FUNCTION trellis_private.research_canonical_json(p_value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
DECLARE v_result text;
BEGIN
    IF jsonb_typeof(p_value)='object' THEN
        SELECT '{'||COALESCE(string_agg(to_jsonb(key)::text||':'||trellis_private.research_canonical_json(value),',' ORDER BY key COLLATE "C"),'')||'}'
            INTO v_result FROM jsonb_each(p_value);
    ELSIF jsonb_typeof(p_value)='array' THEN
        SELECT '['||COALESCE(string_agg(trellis_private.research_canonical_json(value),',' ORDER BY ordinal),'')||']'
            INTO v_result FROM jsonb_array_elements(p_value) WITH ORDINALITY item(value,ordinal);
    ELSE
        v_result:=p_value::text;
    END IF;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.research_canonical_json(jsonb) FROM PUBLIC,anon,authenticated;

ALTER FUNCTION trellis_private.research_mutate(text,jsonb,text) RENAME TO research_mutate_before_publication;
CREATE FUNCTION trellis_private.research_mutate(p_action text,p_payload jsonb,p_capability text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_state trellis.research_runs%ROWTYPE; v_call trellis.model_calls%ROWTYPE;
    v_source trellis.web_sources%ROWTYPE; v_report jsonb:=p_payload->'report'; v_review jsonb:=p_payload->'review';
    v_claim jsonb; v_ref jsonb; v_refs jsonb:='[]'; v_result jsonb; v_fingerprint text; v_hash text;
    v_partial boolean:=(p_payload->>'partial')::boolean; v_markdown text:=v_report->>'markdown';
    v_output trellis.run_assistant_outputs%ROWTYPE; v_now timestamptz:=clock_timestamp();
BEGIN
    IF p_action<>'publish' THEN
        RETURN trellis_private.research_mutate_before_publication(p_action,p_payload,p_capability);
    END IF;
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR octet_length(p_payload::text)>1048576
       OR jsonb_typeof(v_report) IS DISTINCT FROM 'object' OR jsonb_typeof(v_review) IS DISTINCT FROM 'object'
       OR jsonb_typeof(v_report->'claims') IS DISTINCT FROM 'array'
       OR jsonb_typeof(v_report->'covered_question_ids') IS DISTINCT FROM 'array'
       OR jsonb_typeof(v_review->'accepted_claim_ids') IS DISTINCT FROM 'array'
       OR jsonb_typeof(v_review->'issues') IS DISTINCT FROM 'array'
       OR jsonb_typeof(v_review->'source_hashes') IS DISTINCT FROM 'object'
       OR jsonb_typeof(v_report->'markdown') IS DISTINCT FROM 'string'
       OR v_partial IS NULL OR length(btrim(v_markdown))=0 OR octet_length(v_markdown)>65536
       OR COALESCE(length(v_report->>'stop_reason'),0) NOT BETWEEN 1 AND 100
       OR jsonb_array_length(v_report->'claims')>50 OR (SELECT count(*) FROM jsonb_object_keys(v_review->'source_hashes'))>50 THEN
        RAISE EXCEPTION 'research_report_invalid' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE user_id=v_user AND id=(p_payload->>'run_id')::uuid FOR UPDATE;
    IF NOT FOUND OR v_run.run_mode<>'research' THEN RAISE EXCEPTION 'research_not_found' USING ERRCODE='42501'; END IF;
    SELECT * INTO v_state FROM trellis.research_runs WHERE user_id=v_user AND run_id=v_run.id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'research_not_found' USING ERRCODE='22023'; END IF;
    v_fingerprint:=encode(sha256(convert_to((p_payload-'generation')::text,'UTF8')),'hex');
    IF v_state.publication_receipt IS NOT NULL THEN
        IF v_state.publication_fingerprint IS DISTINCT FROM v_fingerprint THEN
            RAISE EXCEPTION 'research_operation_conflict' USING ERRCODE='22023';
        END IF;
        RETURN v_state.publication_receipt;
    END IF;
    v_run:=trellis_private.require_run(v_run.id,p_capability);
    IF v_run.generation IS DISTINCT FROM (p_payload->>'generation')::integer OR v_run.status<>'running'
       OR v_run.cancel_requested_at IS NOT NULL OR v_run.deadline_at<=v_now OR v_state.budget_unknown
       OR EXISTS(SELECT 1 FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id AND status IN ('pending','streaming'))
       OR EXISTS(SELECT 1 FROM trellis.tool_calls WHERE user_id=v_user AND run_id=v_run.id AND status IN ('pending','running')) THEN
        RAISE EXCEPTION 'research_not_quiescent' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_call FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id AND id=(p_payload->>'model_call_id')::uuid;
    IF NOT FOUND OR v_call.status<>'completed' OR v_call.task_id IS NULL THEN
        RAISE EXCEPTION 'research_report_invalid' USING ERRCODE='22023';
    END IF;
    v_hash:=encode(sha256(convert_to(trellis_private.research_canonical_json(v_report),'UTF8')),'hex');
    IF v_hash IS DISTINCT FROM p_payload->>'report_hash' OR v_hash IS DISTINCT FROM v_review->>'report_hash'
       OR (NOT v_partial AND jsonb_array_length(v_review->'issues')>0)
       OR (SELECT count(*) FROM jsonb_array_elements(v_report->'claims'))<>(SELECT count(DISTINCT value->>'id') FROM jsonb_array_elements(v_report->'claims')) THEN
        RAISE EXCEPTION 'research_review_changed' USING ERRCODE='22023';
    END IF;
    PERFORM 1 FROM trellis.chats WHERE user_id=v_user AND id=v_run.chat_id FOR UPDATE;
    FOR v_claim IN SELECT value FROM jsonb_array_elements(v_report->'claims') LOOP
        IF jsonb_typeof(v_claim) IS DISTINCT FROM 'object' OR COALESCE(length(v_claim->>'id'),0) NOT BETWEEN 1 AND 100
           OR COALESCE(length(v_claim->>'text'),0) NOT BETWEEN 1 AND 8000
           OR position(v_claim->>'text' IN v_markdown)=0
           OR NOT v_review->'accepted_claim_ids' @> jsonb_build_array(v_claim->>'id')
           OR jsonb_typeof(v_claim->'citations') IS DISTINCT FROM 'array' OR jsonb_array_length(v_claim->'citations')=0 THEN
            RAISE EXCEPTION 'research_claim_unsupported' USING ERRCODE='22023';
        END IF;
        FOR v_ref IN SELECT value FROM jsonb_array_elements(v_claim->'citations') LOOP
            SELECT * INTO v_source FROM trellis.web_sources WHERE user_id=v_user AND session_id=v_run.chat_id
                AND id=(v_ref->>'source_id')::uuid AND status IN ('success','partial') FOR SHARE;
            IF NOT FOUND OR v_review->'source_hashes'->>(v_source.id::text) IS DISTINCT FROM v_source.content_hash
               OR (v_ref->>'start_line')::integer IS NULL OR (v_ref->>'start_line')::integer<1
               OR (v_ref->>'end_line')::integer IS NULL OR (v_ref->>'end_line')::integer<(v_ref->>'start_line')::integer
               OR (v_ref->>'end_line')::integer>v_source.line_count
               OR (v_ref->>'end_line')::integer-(v_ref->>'start_line')::integer>=200
               OR position('[[source:'||v_source.id::text||':L'||(v_ref->>'start_line')||'-L'||(v_ref->>'end_line')||']]' IN v_markdown)=0 THEN
                RAISE EXCEPTION 'research_source_changed' USING ERRCODE='22023';
            END IF;
            v_refs:=v_refs||jsonb_build_array(v_ref);
        END LOOP;
    END LOOP;
    IF jsonb_array_length(v_refs)>50 THEN RAISE EXCEPTION 'research_report_invalid' USING ERRCODE='22023'; END IF;
    PERFORM trellis_private.web_mutate('bind_citations',jsonb_build_object('run_id',v_run.id,'generation',v_run.generation,
        'model_call_id',v_call.id,'citations',v_refs),p_capability);
    INSERT INTO trellis.run_assistant_outputs(user_id,chat_id,turn_id,run_id,model_call_id,step_index,content,status)
        VALUES(v_user,v_run.chat_id,v_run.turn_id,v_run.id,v_call.id,v_call.step_index,v_markdown,'complete')
        RETURNING * INTO v_output;
    UPDATE trellis.research_runs SET report_model_call_id=v_call.id,report=v_report,review=v_review,
        phase=CASE WHEN v_partial THEN 'partial' ELSE 'completed' END,stop_reason=v_report->>'stop_reason',
        revision=revision+1,updated_at=v_now WHERE user_id=v_user AND run_id=v_run.id;
    v_result:=trellis_private.mutate('complete_run',jsonb_build_object('run_id',v_run.id,'content',v_markdown),p_capability);
    UPDATE trellis.runs SET stop_reason=v_report->>'stop_reason' WHERE user_id=v_user AND id=v_run.id RETURNING * INTO v_run;
    UPDATE trellis.run_assistant_outputs SET stop_reason=v_run.stop_reason WHERE id=v_output.id;
    v_result:=jsonb_build_object('run',trellis_private.public_run(to_jsonb(v_run)),'events',COALESCE(v_result->'events','[]'::jsonb));
    UPDATE trellis.research_runs SET publication_receipt=v_result,publication_fingerprint=v_fingerprint
        WHERE user_id=v_user AND run_id=v_run.id;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.research_mutate_before_publication(text,jsonb,text) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION trellis_private.research_mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.research_mutate(text,jsonb,text) TO authenticated;

CREATE OR REPLACE FUNCTION trellis.research_readiness() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('schema_version',1,'ready',
        to_regprocedure('trellis_private.research_mutate_before_publication(text,jsonb,text)') IS NOT NULL
        AND to_regprocedure('trellis_private.research_mutate_before_clarification(text,jsonb,text)') IS NOT NULL
        AND to_regprocedure('trellis_private.research_canonical_json(jsonb)') IS NOT NULL)
$$;

COMMIT;
