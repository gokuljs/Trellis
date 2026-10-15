-- Bounded browser history. Completed-message reads remain compatible.
BEGIN;
CREATE INDEX outputs_unpromoted_timeline ON trellis.run_assistant_outputs
    (user_id,chat_id,created_at DESC,id DESC) WHERE promoted_message_id IS NULL;
CREATE UNIQUE INDEX output_promoted_message ON trellis.run_assistant_outputs
    (user_id,promoted_message_id) WHERE promoted_message_id IS NOT NULL;
CREATE INDEX runs_chat_turn_created ON trellis.runs(user_id,chat_id,turn_id,created_at DESC,id DESC);
CREATE INDEX messages_chat_timeline ON trellis.messages(user_id,chat_id,created_at DESC,id DESC);

CREATE FUNCTION trellis.read_chat_history(p_chat_id uuid,p_limit integer DEFAULT 40,
    p_before_time timestamptz DEFAULT NULL,p_before_kind text DEFAULT NULL,p_before_id uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE v_result jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
       OR (p_before_time IS NULL)<>(p_before_id IS NULL)
       OR (p_before_time IS NULL)<>(p_before_kind IS NULL)
       OR (p_before_kind IS NOT NULL AND p_before_kind NOT IN ('message','assistant_output')) THEN
        RAISE EXCEPTION 'invalid history page' USING ERRCODE='22023';
    END IF;
    WITH candidates AS MATERIALIZED (
        SELECT created_at,'message'::text AS kind,id FROM trellis.messages
        WHERE user_id=(SELECT auth.uid()) AND chat_id=p_chat_id
          AND (p_before_time IS NULL OR (created_at,'message'::text,id)<(p_before_time,p_before_kind,p_before_id))
        ORDER BY created_at DESC,id DESC LIMIT p_limit+1
    ), output_candidates AS MATERIALIZED (
        SELECT created_at,'assistant_output'::text AS kind,id FROM trellis.run_assistant_outputs
        WHERE user_id=(SELECT auth.uid()) AND chat_id=p_chat_id AND promoted_message_id IS NULL
          AND (p_before_time IS NULL OR (created_at,'assistant_output'::text,id)<(p_before_time,p_before_kind,p_before_id))
        ORDER BY created_at DESC,id DESC LIMIT p_limit+1
    ), combined AS MATERIALIZED (
        SELECT * FROM candidates UNION ALL SELECT * FROM output_candidates
    ), page AS MATERIALIZED (
        SELECT * FROM combined ORDER BY created_at DESC,kind DESC,id DESC LIMIT p_limit
    )
    SELECT jsonb_build_object(
        'messages',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.created_at,m.id)
            FROM trellis.messages m JOIN page p ON p.id=m.id AND p.kind='message'
            WHERE m.user_id=(SELECT auth.uid()) AND m.chat_id=p_chat_id),'[]'::jsonb),
        'assistant_outputs',COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.created_at,o.id)
            FROM trellis.run_assistant_outputs o WHERE o.user_id=(SELECT auth.uid()) AND o.chat_id=p_chat_id
            AND (o.id IN (SELECT id FROM page WHERE kind='assistant_output')
                 OR o.promoted_message_id IN (SELECT id FROM page WHERE kind='message'))),'[]'::jsonb),
        'timeline_order',COALESCE((SELECT jsonb_agg(jsonb_build_object('kind',kind,'id',id)
            ORDER BY created_at,kind,id) FROM page),'[]'::jsonb),
        'next_before',CASE WHEN (SELECT count(*) FROM combined)>p_limit THEN
            (SELECT jsonb_build_object('created_at',created_at,'kind',kind,'id',id)
             FROM page ORDER BY created_at,kind,id LIMIT 1) ELSE NULL END
    ) INTO v_result;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis.read_chat_history(uuid,integer,timestamptz,text,uuid) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.read_chat_history(uuid,integer,timestamptz,text,uuid) TO authenticated;

CREATE FUNCTION trellis.read_chat_summaries_page(p_limit integer DEFAULT 40,
    p_before_time timestamptz DEFAULT NULL,p_before_id uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE v_result jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
       OR (p_before_time IS NULL)<>(p_before_id IS NULL) THEN
        RAISE EXCEPTION 'invalid session page' USING ERRCODE='22023';
    END IF;
    WITH candidates AS MATERIALIZED (
        SELECT * FROM trellis.chats WHERE user_id=(SELECT auth.uid())
          AND (p_before_time IS NULL OR (updated_at,id)<(p_before_time,p_before_id))
        ORDER BY updated_at DESC,id DESC LIMIT p_limit+1
    ), page AS MATERIALIZED (
        SELECT * FROM candidates ORDER BY updated_at DESC,id DESC LIMIT p_limit
    )
    SELECT jsonb_build_object('items',COALESCE((SELECT jsonb_agg(to_jsonb(c)||jsonb_build_object(
        'message_count',(SELECT count(*) FROM trellis.messages m WHERE m.user_id=c.user_id AND m.chat_id=c.id))
        ORDER BY c.updated_at DESC,c.id DESC) FROM page c),'[]'::jsonb),
        'next_before',CASE WHEN (SELECT count(*) FROM candidates)>p_limit THEN
            (SELECT jsonb_build_object('created_at',updated_at,'kind','session','id',id)
            FROM page ORDER BY updated_at,id LIMIT 1) ELSE NULL END) INTO v_result;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis.read_chat_summaries_page(integer,timestamptz,uuid) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.read_chat_summaries_page(integer,timestamptz,uuid) TO authenticated;
COMMIT;
