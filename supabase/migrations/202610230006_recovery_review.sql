BEGIN;

CREATE OR REPLACE FUNCTION trellis_private.append_event(
    p_user uuid,p_run_id uuid,p_event_type text,p_data jsonb,p_event_version integer DEFAULT 1
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_run trellis.runs%ROWTYPE; v_event trellis.run_events%ROWTYPE; v_data jsonb:=p_data;
    v_size bigint; v_reserved boolean:=p_event_type IN ('run.cancellation_requested','assistant.completed',
        'run.completed','run.failed','run.cancelled','run.interrupted');
    v_count_limit bigint:=CASE WHEN v_reserved THEN 8192 ELSE 8176 END;
    v_byte_limit bigint:=CASE WHEN v_reserved THEN 16777216 ELSE 16711680 END;
BEGIN
    SELECT * INTO v_run FROM trellis.runs WHERE id=p_run_id AND user_id=p_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='22023'; END IF;
    IF jsonb_typeof(v_data) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'run event data must be an object' USING ERRCODE='22023';
    END IF;
    IF p_event_type IN ('run.completed','run.failed','run.cancelled','run.interrupted') THEN
        v_data:=v_data||jsonb_build_object('retry_allowed',v_run.retry_allowed,
            'recovery_reason',v_run.recovery_reason,'uncertain_tool_call_id',v_run.uncertain_tool_call_id);
    END IF;
    v_size:=octet_length(v_data::text);
    IF v_size>262144 OR v_run.event_storage_count+1>v_count_limit
       OR v_run.last_event_sequence+1>v_count_limit OR v_run.event_storage_bytes+v_size>v_byte_limit THEN
        RAISE EXCEPTION 'event_storage_limit' USING ERRCODE='22023';
    END IF;
    UPDATE trellis.runs SET last_event_sequence=last_event_sequence+1,
        event_storage_count=event_storage_count+1,event_storage_bytes=event_storage_bytes+v_size
        WHERE id=p_run_id AND user_id=p_user;
    INSERT INTO trellis.run_events(user_id,run_id,sequence,event_type,event_version,data)
        VALUES(p_user,p_run_id,v_run.last_event_sequence+1,p_event_type,p_event_version,v_data) RETURNING * INTO v_event;
    RETURN to_jsonb(v_event);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.append_event(uuid,uuid,text,jsonb,integer)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

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
                AND c.status NOT IN ('pending','streaming')) AS usage_complete,
            sum(c.estimated_cost) AS estimated_cost_usd,
            bool_and(c.estimated_cost IS NOT NULL
                AND c.status NOT IN ('pending','streaming')) AS cost_complete
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


ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_recovery_review;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE; v_tool trellis.tool_calls%ROWTYPE;
BEGIN
    IF p_action<>'acknowledge_workspace_review' THEN
        RETURN trellis_private.mutate_before_recovery_review(p_action,p_payload,p_capability);
    END IF;
    IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' THEN
        RAISE EXCEPTION 'invalid workspace review acknowledgment' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE id=(p_payload->>'run_id')::uuid AND user_id=v_user
        AND chat_id=(p_payload->>'session_id')::uuid FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'workspace review run not found' USING ERRCODE='42501'; END IF;
    IF v_run.status NOT IN ('completed','failed','cancelled','interrupted') THEN
        RAISE EXCEPTION 'workspace review requires a stopped run' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_tool FROM trellis.tool_calls WHERE id=(p_payload->>'tool_call_id')::uuid AND user_id=v_user
        AND run_id=v_run.id FOR UPDATE;
    IF NOT FOUND OR v_tool.name<>'run_command' OR v_tool.status<>'outcome_unknown' THEN
        RAISE EXCEPTION 'workspace review requires the uncertain command identity' USING ERRCODE='22023';
    END IF;
    UPDATE trellis.runs SET retry_allowed=false,uncertain_tool_call_id=COALESCE(uncertain_tool_call_id,v_tool.id),
        recovery_reason=COALESCE(recovery_reason,'tool_outcome_unknown') WHERE id=v_run.id AND user_id=v_user;
    IF v_tool.reviewed_at IS NULL THEN
        UPDATE trellis.tool_calls SET reviewed_at=clock_timestamp()
            WHERE id=v_tool.id AND user_id=v_user RETURNING * INTO v_tool;
        BEGIN
            PERFORM trellis_private.append_event(v_user,v_run.id,'run.phase',jsonb_build_object(
                'phase','tool_use','review_acknowledged',true,'tool_call_id',v_tool.id,
                'reviewed_at',v_tool.reviewed_at,'reviewed_by',v_user));
        EXCEPTION WHEN invalid_parameter_value THEN
            IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
        END;
    END IF;
    RETURN trellis_private.public_tool_call(to_jsonb(v_tool));
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_recovery_review(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

COMMIT;
