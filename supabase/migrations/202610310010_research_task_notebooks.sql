BEGIN;

CREATE FUNCTION trellis_private.research_task_notebook(p_checkpoint jsonb,p_result jsonb)
RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path='' AS $$
    SELECT COALESCE(p_checkpoint,'{}'::jsonb)||COALESCE(p_result,'{}'::jsonb)
$$;
REVOKE ALL ON FUNCTION trellis_private.research_task_notebook(jsonb,jsonb) FROM PUBLIC,anon,authenticated;

CREATE OR REPLACE FUNCTION trellis_private.research_mutate(p_action text,p_payload jsonb,p_capability text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_state trellis.research_runs%ROWTYPE; v_coverage jsonb; v_item jsonb; v_question jsonb;
    v_required jsonb; v_ref jsonb; v_claim jsonb; v_claim_ids jsonb; v_question_ids jsonb;
    v_findings jsonb; v_snapshot jsonb; v_requirement jsonb;
BEGIN
    IF p_action='publish' THEN
        SELECT * INTO v_run FROM trellis.runs WHERE user_id=v_user AND id=(p_payload->>'run_id')::uuid FOR UPDATE;
        IF v_run.research_policy_version='bounded-research-v2' THEN
            SELECT * INTO v_state FROM trellis.research_runs WHERE user_id=v_user AND run_id=v_run.id FOR UPDATE;
            IF v_state.publication_receipt IS NOT NULL THEN
                RETURN trellis_private.research_mutate_before_quality(p_action,p_payload,p_capability);
            END IF;
            v_coverage:=p_payload#>'{review,question_coverage}';
            SELECT COALESCE(jsonb_agg(value->>'id'),'[]'::jsonb) INTO v_claim_ids
                FROM jsonb_array_elements(p_payload#>'{report,claims}');
            SELECT COALESCE(jsonb_agg(value->>'id'),'[]'::jsonb) INTO v_question_ids
                FROM jsonb_array_elements(v_state.checkpoint->'subquestions');
            IF jsonb_typeof(v_coverage) IS DISTINCT FROM 'array' OR jsonb_array_length(v_coverage)>12
                OR NOT v_claim_ids @> (p_payload#>'{review,accepted_claim_ids}')
                OR NOT v_question_ids @> (p_payload#>'{report,covered_question_ids}')
                OR (SELECT count(*) FROM jsonb_array_elements(v_coverage))<>
                   (SELECT count(DISTINCT value->>'question_id') FROM jsonb_array_elements(v_coverage))
                THEN RAISE EXCEPTION 'research_review_changed' USING ERRCODE='22023'; END IF;
            v_snapshot:=trellis_private.research_snapshot(v_run.id);
            SELECT COALESCE(jsonb_agg(finding),'[]'::jsonb) INTO v_findings FROM (
                SELECT value AS finding FROM jsonb_array_elements(COALESCE(v_state.checkpoint->'findings','[]'::jsonb))
                UNION ALL
                SELECT finding FROM trellis.research_tasks task,
                    jsonb_array_elements(COALESCE(trellis_private.research_task_notebook(task.checkpoint,task.result)->'findings','[]'::jsonb)) finding
                WHERE task.user_id=v_user AND task.run_id=v_run.id AND task.role IN ('lead','worker')
            ) findings;
            FOR v_item IN SELECT value FROM jsonb_array_elements(v_coverage) LOOP
                SELECT value INTO v_question FROM jsonb_array_elements(v_state.checkpoint->'subquestions')
                    WHERE value->>'id'=v_item->>'question_id';
                IF v_question IS NULL OR jsonb_typeof(v_item->'requirement_ids') IS DISTINCT FROM 'array'
                    OR jsonb_typeof(v_item->'claim_ids') IS DISTINCT FROM 'array'
                    OR jsonb_array_length(v_item->'requirement_ids')=0
                    OR jsonb_array_length(v_item->'claim_ids')=0
                    OR NOT (p_payload#>'{review,accepted_claim_ids}') @> (v_item->'claim_ids')
                    THEN RAISE EXCEPTION 'research_review_changed' USING ERRCODE='22023'; END IF;
                SELECT COALESCE(jsonb_agg(value->>'id'),'[]'::jsonb) INTO v_required
                    FROM jsonb_array_elements(v_question->'evidence_requirements');
                IF NOT v_required @> (v_item->'requirement_ids') THEN
                    RAISE EXCEPTION 'research_review_changed' USING ERRCODE='22023'; END IF;
                FOR v_requirement IN SELECT value FROM jsonb_array_elements(v_question->'evidence_requirements')
                    WHERE (v_item->'requirement_ids') @> jsonb_build_array(value->>'id') LOOP
                    IF NOT trellis_private.research_requirement_supported(
                        v_question->>'id',v_requirement,v_item->'claim_ids',p_payload#>'{report,claims}',
                        v_findings,v_snapshot->'source_reads',p_payload#>'{review,source_hashes}') THEN
                        RAISE EXCEPTION 'research_claim_unsupported' USING ERRCODE='22023';
                    END IF;
                END LOOP;
            END LOOP;
            IF NOT (p_payload->>'partial')::boolean AND (
                jsonb_typeof(v_state.checkpoint->'subquestions') IS DISTINCT FROM 'array'
                OR jsonb_array_length(v_state.checkpoint->'subquestions')=0) THEN
                RAISE EXCEPTION 'research_claim_unsupported' USING ERRCODE='22023'; END IF;
            FOR v_question IN SELECT value FROM jsonb_array_elements(v_state.checkpoint->'subquestions') LOOP
                IF (p_payload->>'partial')::boolean AND NOT (p_payload#>'{report,covered_question_ids}') @> jsonb_build_array(v_question->>'id') THEN CONTINUE; END IF;
                SELECT jsonb_agg(value->>'id') INTO v_required
                    FROM jsonb_array_elements(v_question->'evidence_requirements');
                IF v_required IS NULL OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(v_coverage) c
                    WHERE c->>'question_id'=v_question->>'id' AND (c->'requirement_ids') @> v_required)
                    OR NOT (p_payload#>'{report,covered_question_ids}') @> jsonb_build_array(v_question->>'id')
                    THEN RAISE EXCEPTION 'research_claim_unsupported' USING ERRCODE='22023'; END IF;
            END LOOP;
            FOR v_claim IN SELECT value FROM jsonb_array_elements(p_payload#>'{report,claims}') LOOP
                FOR v_ref IN SELECT value FROM jsonb_array_elements(v_claim->'citations') LOOP
                    IF NOT EXISTS(SELECT 1 FROM trellis.web_sources WHERE user_id=v_user AND run_id=v_run.id
                        AND id::text=v_ref->>'source_id') THEN
                        RAISE EXCEPTION 'research_source_changed' USING ERRCODE='22023'; END IF;
                END LOOP;
            END LOOP;
        END IF;
    END IF;
    RETURN trellis_private.research_mutate_before_quality(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.research_mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.research_mutate(text,jsonb,text) TO authenticated;

COMMIT;
