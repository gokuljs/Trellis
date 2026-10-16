BEGIN;

CREATE FUNCTION trellis.read_session_run_summaries(p_chat_id uuid,p_offset integer DEFAULT 0,
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
            waiting_tool_call_id
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

COMMIT;
