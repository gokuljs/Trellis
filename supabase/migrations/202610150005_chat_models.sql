BEGIN;
ALTER TABLE trellis.chats ADD COLUMN model_id text REFERENCES trellis.models(id);
UPDATE trellis.chats c SET model_id=s.selected_model_id FROM trellis.user_settings s WHERE s.user_id=c.user_id;
CREATE FUNCTION trellis_private.seed_chat_model() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF NEW.model_id IS NULL THEN
        SELECT selected_model_id INTO NEW.model_id FROM trellis.user_settings WHERE user_id=NEW.user_id;
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.seed_chat_model() FROM PUBLIC,anon,authenticated;
CREATE TRIGGER seed_chat_model BEFORE INSERT ON trellis.chats FOR EACH ROW EXECUTE FUNCTION trellis_private.seed_chat_model();

DROP FUNCTION trellis.list_chat_summaries(uuid);
CREATE FUNCTION trellis.list_chat_summaries(p_chat_id uuid DEFAULT NULL)
RETURNS TABLE(id uuid,user_id uuid,title text,workspace_path text,created_at timestamptz,
    updated_at timestamptz,message_count bigint,model_id text)
LANGUAGE sql STABLE SECURITY INVOKER AS $$
    SELECT c.id,c.user_id,c.title,c.workspace_path,c.created_at,c.updated_at,
        (SELECT count(*) FROM trellis.messages m WHERE m.user_id=c.user_id AND m.chat_id=c.id),c.model_id
    FROM trellis.chats c WHERE (SELECT auth.uid()) IS NOT NULL AND c.user_id=(SELECT auth.uid())
        AND (p_chat_id IS NULL OR c.id=p_chat_id)
$$;
REVOKE ALL ON FUNCTION trellis.list_chat_summaries(uuid) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.list_chat_summaries(uuid) TO authenticated;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_chat_models;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_user uuid:=auth.uid(); v_chat trellis.chats%ROWTYPE;
BEGIN
    IF v_user IS NULL THEN RAISE EXCEPTION 'authentication required' USING ERRCODE='42501'; END IF;
    IF p_action='set_session_model' THEN
        SELECT * INTO v_chat FROM trellis.chats WHERE id=(p_payload->>'session_id')::uuid AND user_id=v_user FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF EXISTS(SELECT 1 FROM trellis.runs WHERE user_id=v_user AND chat_id=v_chat.id
            AND status IN ('queued','running','waiting_for_approval','cancelling'))
           OR EXISTS(SELECT 1 FROM trellis.turn_claims WHERE user_id=v_user AND chat_id=v_chat.id
                AND claimed_at>clock_timestamp()-interval '5 minutes') THEN
            RAISE EXCEPTION 'session_model_busy' USING ERRCODE='PT409';
        END IF;
        IF NOT EXISTS(SELECT 1 FROM trellis.models WHERE id=p_payload->>'model_id' AND enabled
            AND metadata_status='ready' AND availability<>'unavailable') THEN
            RAISE EXCEPTION 'model_not_available' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.chats SET model_id=p_payload->>'model_id',updated_at=now()
        WHERE id=v_chat.id AND user_id=v_user RETURNING * INTO v_chat;
        RETURN to_jsonb(v_chat);
    END IF;
    RETURN trellis_private.mutate_before_chat_models(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_chat_models(text,jsonb,text) FROM PUBLIC,anon,authenticated;
COMMIT;
