BEGIN;

CREATE FUNCTION trellis_private.research_read_observation(
    p_message trellis.run_messages,p_tool trellis.tool_calls,p_source trellis.web_sources
) RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path='' AS $$
DECLARE
    v_data jsonb; v_item jsonb; v_start integer; v_end integer; v_number integer;
    v_presented integer[]:='{}'; v_claimed integer[]:='{}'; v_source_lines text[];
BEGIN
    IF p_tool.name<>'web_source_read' OR p_tool.status<>'completed'
        OR p_message.role<>'tool' OR p_message.tool_call_id<>p_tool.id
        OR p_message.task_id IS DISTINCT FROM p_tool.task_id OR p_tool.task_id IS NULL
        OR p_source.user_id<>p_tool.user_id OR p_source.run_id<>p_tool.run_id
        OR p_message.user_id<>p_tool.user_id OR p_message.run_id<>p_tool.run_id
        OR p_source.status NOT IN ('success','partial') THEN RETURN NULL; END IF;
    v_data:=p_message.content::jsonb;
    IF v_data->>'source_id' IS DISTINCT FROM p_source.id::text
        OR v_data->>'content_hash' IS DISTINCT FROM p_source.content_hash
        OR v_data->>'read_kind' IS DISTINCT FROM 'source_lines'
        OR v_data->'substantive' IS DISTINCT FROM 'true'::jsonb
        OR v_data->>'status' NOT IN ('success','partial')
        OR jsonb_typeof(v_data->'presented_ranges') IS DISTINCT FROM 'array'
        OR jsonb_typeof(v_data->'untrusted_lines') IS DISTINCT FROM 'array'
        OR jsonb_array_length(v_data->'untrusted_lines') NOT BETWEEN 1 AND 200
        OR jsonb_array_length(v_data->'presented_ranges') NOT BETWEEN 1 AND 200
        THEN RETURN NULL; END IF;
    v_source_lines:=string_to_array(p_source.text,E'\n');
    FOR v_item IN SELECT value FROM jsonb_array_elements(v_data->'untrusted_lines') LOOP
        v_number:=(v_item->>'line')::integer;
        IF v_number IS NULL OR v_number NOT BETWEEN 1 AND p_source.line_count
            OR jsonb_typeof(v_item->'text') IS DISTINCT FROM 'string'
            OR v_item->>'text' IS DISTINCT FROM v_source_lines[v_number]
            OR v_number=ANY(v_presented) THEN RETURN NULL; END IF;
        v_presented:=array_append(v_presented,v_number);
    END LOOP;
    FOR v_item IN SELECT value FROM jsonb_array_elements(v_data->'presented_ranges') LOOP
        v_start:=(v_item->>'start_line')::integer; v_end:=(v_item->>'end_line')::integer;
        IF v_start IS NULL OR v_end IS NULL OR v_start<1 OR v_end<v_start
            OR v_end>p_source.line_count OR v_end-v_start>=200 THEN RETURN NULL; END IF;
        FOR v_number IN v_start..v_end LOOP v_claimed:=array_append(v_claimed,v_number); END LOOP;
    END LOOP;
    IF (SELECT array_agg(DISTINCT n ORDER BY n) FROM unnest(v_presented) n)
        IS DISTINCT FROM (SELECT array_agg(DISTINCT n ORDER BY n) FROM unnest(v_claimed) n)
        THEN RETURN NULL; END IF;
    RETURN jsonb_build_object('source_id',p_source.id,'content_hash',p_source.content_hash,
        'tool_call_id',p_tool.id,'task_id',p_tool.task_id,'message_ordinal',p_message.ordinal,
        'read_kind','source_lines','presented_ranges',v_data->'presented_ranges');
EXCEPTION WHEN data_exception THEN RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.research_read_observation(
    trellis.run_messages,trellis.tool_calls,trellis.web_sources) FROM PUBLIC,anon,authenticated;

CREATE OR REPLACE FUNCTION trellis_private.research_snapshot(p_run_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('run_id',s.run_id,'user_id',s.user_id,'session_id',s.session_id,
        'policy_version',r.research_policy_version,'execution_budget_seconds',r.execution_budget_seconds,
        'objective',s.objective,'phase',s.phase,'revision',s.revision,'checkpoint',s.checkpoint,
        'pending_clarification',s.pending_clarification,'stop_reason',COALESCE(s.stop_reason,r.stop_reason),
        'report_model_call_id',s.report_model_call_id,
        'reviewed_coverage',COALESCE(NULLIF(s.review->'question_coverage','null'::jsonb),'[]'::jsonb),
        'source_reads',CASE WHEN r.research_policy_version='bounded-research-v2' THEN COALESCE((
            SELECT jsonb_agg(observation ORDER BY ordinal) FROM (
                SELECT m.ordinal,trellis_private.research_read_observation(m,t,w) AS observation
                FROM trellis.tool_calls t JOIN trellis.run_messages m
                    ON m.user_id=t.user_id AND m.run_id=t.run_id AND m.tool_call_id=t.id
                JOIN trellis.web_sources w ON w.user_id=t.user_id AND w.run_id=t.run_id
                    AND w.id::text=t.arguments->>'source_id'
                WHERE t.user_id=s.user_id AND t.run_id=s.run_id AND t.name='web_source_read'
                    AND t.status='completed' ORDER BY m.ordinal LIMIT 384
            ) reads WHERE observation IS NOT NULL),'[]'::jsonb) ELSE '[]'::jsonb END,
        'remaining_seconds',COALESCE(s.paused_remaining_seconds,GREATEST(0,extract(epoch FROM r.deadline_at-statement_timestamp()))),
        'tasks',COALESCE((SELECT jsonb_agg(to_jsonb(t)-'user_id' ORDER BY t.created_at,t.id)
            FROM trellis.research_tasks t WHERE t.user_id=s.user_id AND t.run_id=s.run_id),'[]'::jsonb),
        'citations',COALESCE((SELECT jsonb_agg(jsonb_build_object('source_id',c.source_id,
            'start_line',c.start_line,'end_line',c.end_line) ORDER BY c.citation_index)
            FROM trellis.web_citations c WHERE c.user_id=s.user_id AND c.model_call_id=s.report_model_call_id),'[]'::jsonb))
    FROM trellis.research_runs s JOIN trellis.runs r ON r.user_id=s.user_id AND r.id=s.run_id
    WHERE s.user_id=(SELECT auth.uid()) AND s.run_id=p_run_id
$$;

CREATE FUNCTION trellis_private.research_requirement_supported(
    p_question_id text,p_requirement jsonb,p_claim_ids jsonb,p_claims jsonb,
    p_findings jsonb,p_reads jsonb,p_hashes jsonb
) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
DECLARE
    v_finding jsonb; v_ref jsonb; v_seen int4multirange; v_start integer; v_end integer;
    v_observed boolean; v_overlap boolean; v_depth text:=COALESCE(p_requirement->>'depth','source_excerpt');
BEGIN
    IF v_depth NOT IN ('source_excerpt','abstract','full_text','listing') THEN RETURN false; END IF;
    FOR v_finding IN SELECT value FROM jsonb_array_elements(p_findings) LOOP
        IF NOT COALESCE(v_finding->'question_ids' @> jsonb_build_array(p_question_id),false)
            OR NOT COALESCE(v_finding->'requirement_ids' @> jsonb_build_array(p_requirement->>'id'),false)
            OR (v_depth<>'source_excerpt' AND v_finding->>'evidence_depth' IS DISTINCT FROM v_depth)
            OR jsonb_typeof(v_finding->'citations') IS DISTINCT FROM 'array'
            OR jsonb_array_length(v_finding->'citations')=0 THEN CONTINUE; END IF;
        v_observed:=true; v_overlap:=false;
        FOR v_ref IN SELECT value FROM jsonb_array_elements(v_finding->'citations') LOOP
            v_start:=(v_ref->>'start_line')::integer; v_end:=(v_ref->>'end_line')::integer;
            IF v_start IS NULL OR v_end IS NULL OR v_start<1 OR v_end<v_start OR v_end-v_start>=200 THEN
                v_observed:=false; EXIT;
            END IF;
            SELECT range_agg(int4range((r->>'start_line')::integer,(r->>'end_line')::integer+1,'[)')) INTO v_seen
                FROM jsonb_array_elements(p_reads) observation,
                    jsonb_array_elements(observation->'presented_ranges') r
                WHERE observation->>'source_id'=v_ref->>'source_id'
                    AND observation->>'content_hash'=p_hashes->>(v_ref->>'source_id');
            IF v_seen IS NULL OR NOT (v_seen @> int4range(v_start,v_end+1,'[)')) THEN
                v_observed:=false; EXIT;
            END IF;
            IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_claims) claim,
                jsonb_array_elements(claim->'citations') citation
                WHERE p_claim_ids @> jsonb_build_array(claim->>'id')
                    AND citation->>'source_id'=v_ref->>'source_id'
                    AND (citation->>'start_line')::integer<=v_end
                    AND (citation->>'end_line')::integer>=v_start) THEN
                v_overlap:=true;
            END IF;
        END LOOP;
        IF v_observed AND v_overlap THEN RETURN true; END IF;
    END LOOP;
    RETURN false;
EXCEPTION WHEN data_exception THEN RETURN false;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.research_requirement_supported(
    text,jsonb,jsonb,jsonb,jsonb,jsonb,jsonb) FROM PUBLIC,anon,authenticated;

ALTER FUNCTION trellis_private.research_mutate(text,jsonb,text) RENAME TO research_mutate_before_quality;
CREATE FUNCTION trellis_private.research_mutate(p_action text,p_payload jsonb,p_capability text)
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
                    jsonb_array_elements(COALESCE(COALESCE(task.result,task.checkpoint)->'findings','[]'::jsonb)) finding
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
REVOKE ALL ON FUNCTION trellis_private.research_mutate_before_quality(text,jsonb,text) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION trellis_private.research_mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.research_mutate(text,jsonb,text) TO authenticated;

COMMIT;
