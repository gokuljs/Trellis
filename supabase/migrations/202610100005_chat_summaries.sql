-- Return owned chats and visible message counts in one request. This read RPC
-- uses the caller's table grants and FORCE RLS; it has no elevated privileges.
BEGIN;

-- Match the API's stable page order, including the UUID tie-breaker.
DROP INDEX trellis.chats_user_updated;
CREATE INDEX chats_user_updated
    ON trellis.chats(user_id, updated_at DESC, id DESC);

CREATE FUNCTION trellis.list_chat_summaries(p_chat_id uuid DEFAULT NULL)
RETURNS TABLE (
    id uuid,
    user_id uuid,
    title text,
    workspace_path text,
    created_at timestamptz,
    updated_at timestamptz,
    message_count bigint
)
-- No SET clause: PostgreSQL can inline the function and push the API's
-- limit/offset into the indexed chat scan. All referenced objects are qualified.
LANGUAGE sql STABLE SECURITY INVOKER AS $$
    SELECT c.id, c.user_id, c.title, c.workspace_path,
           c.created_at, c.updated_at,
           (SELECT pg_catalog.count(*) FROM trellis.messages AS m
            WHERE m.user_id = c.user_id AND m.chat_id = c.id) AS message_count
    FROM trellis.chats AS c
    WHERE (SELECT auth.uid()) IS NOT NULL
      AND c.user_id = (SELECT auth.uid())
      AND (p_chat_id IS NULL OR c.id = p_chat_id)
$$;

REVOKE ALL ON FUNCTION trellis.list_chat_summaries(uuid) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION trellis.list_chat_summaries(uuid) TO authenticated;

COMMIT;
