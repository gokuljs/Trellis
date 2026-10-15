-- Reviewed metadata determines runtime eligibility. Provider discovery records
-- availability observations; request-time dispatch checks provider credentials.
BEGIN;
ALTER TABLE trellis.models DROP CONSTRAINT enabled_models_are_ready;
ALTER TABLE trellis.models ADD CONSTRAINT enabled_models_are_ready
    CHECK(NOT enabled OR metadata_status='ready');
UPDATE trellis.models SET enabled=true WHERE metadata_status='ready';

CREATE OR REPLACE FUNCTION trellis_private.sync_model_catalog(p_provider text,p_available_ids jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE
    v_id text;
    v_document jsonb;
    v_order integer;
    v_now timestamptz := now();
BEGIN
    IF p_provider IS NULL OR p_provider NOT IN ('openai','anthropic')
       OR jsonb_typeof(p_available_ids) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'invalid catalog snapshot' USING ERRCODE='22023';
    END IF;
    IF jsonb_array_length(p_available_ids)>2000
       OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_available_ids) value
            WHERE jsonb_typeof(value)<>'string'
              OR (value#>>'{}') !~ '^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,199}$') THEN
        RAISE EXCEPTION 'invalid catalog snapshot' USING ERRCODE='22023';
    END IF;
    -- Two workers cannot interleave snapshots or race the unique display order.
    PERFORM pg_advisory_xact_lock(hashtext('trellis.model_catalog'));
    UPDATE trellis.models SET availability='unavailable',availability_checked_at=v_now
    WHERE provider_id=p_provider AND NOT (p_available_ids ? upstream_model_id);
    -- Pending discoveries cannot be selected. Remove absent rows unless an old
    -- account/run references one, preserving all historical foreign keys.
    DELETE FROM trellis.models m WHERE m.provider_id=p_provider AND m.metadata_status='pending'
        AND m.availability='unavailable'
        AND NOT EXISTS(SELECT 1 FROM trellis.user_settings s WHERE s.selected_model_id=m.id)
        AND NOT EXISTS(SELECT 1 FROM trellis.runs r WHERE r.model_id=m.id)
        AND NOT EXISTS(SELECT 1 FROM trellis.model_calls c WHERE c.model_id=m.id)
        AND NOT EXISTS(SELECT 1 FROM trellis.chats c WHERE to_jsonb(c)->>'model_id'=m.id);
    SELECT COALESCE(max(catalog_order),0)+1 INTO v_order FROM trellis.models;
    FOR v_id IN SELECT DISTINCT value FROM jsonb_array_elements_text(p_available_ids) value ORDER BY value LOOP
        SELECT document INTO v_document FROM trellis_private.model_profiles
        WHERE id=p_provider||':'||v_id AND document->>'provider_id'=p_provider
            AND document->>'upstream_model_id'=v_id;
        INSERT INTO trellis.models(id,provider_id,provider_name,adapter_kind,upstream_model_id,name,
            requires_api_key,supports_streaming,supports_tools,enabled,catalog_order,
            trusted_metadata,metadata_status,metadata_verified_at,availability,availability_checked_at)
        VALUES(p_provider||':'||v_id,p_provider,
            CASE p_provider WHEN 'openai' THEN 'OpenAI' ELSE 'Anthropic' END,p_provider,v_id,
            COALESCE(v_document->>'name',v_id),true,v_document IS NOT NULL,v_document IS NOT NULL,
            v_document IS NOT NULL,v_order,v_document,
            CASE WHEN v_document IS NULL THEN 'pending' ELSE 'ready' END,
            CASE WHEN v_document IS NULL THEN NULL ELSE (v_document->>'as_of')::date END,
            'available',v_now)
        ON CONFLICT(id) DO UPDATE SET
            provider_id=excluded.provider_id,provider_name=excluded.provider_name,
            adapter_kind=excluded.adapter_kind,upstream_model_id=excluded.upstream_model_id,
            name=excluded.name,requires_api_key=excluded.requires_api_key,
            supports_streaming=excluded.supports_streaming,supports_tools=excluded.supports_tools,
            enabled=excluded.enabled,trusted_metadata=excluded.trusted_metadata,
            metadata_status=excluded.metadata_status,metadata_verified_at=excluded.metadata_verified_at,
            availability=excluded.availability,availability_checked_at=excluded.availability_checked_at;
        v_order:=v_order+1;
    END LOOP;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.sync_model_catalog(text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.sync_model_catalog(text,jsonb) TO trellis_catalog_sync;

-- Migration 006 renamed the chat-model dispatcher. Replace that implementation
-- so the outer checkpoint, usage, and terminal-call wrappers remain in place.
CREATE OR REPLACE FUNCTION trellis_private.mutate_before_context_checkpoints(
    p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
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
            AND metadata_status='ready') THEN
            RAISE EXCEPTION 'model_not_available' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.chats SET model_id=p_payload->>'model_id',updated_at=now()
        WHERE id=v_chat.id AND user_id=v_user RETURNING * INTO v_chat;
        RETURN to_jsonb(v_chat);
    END IF;
    RETURN trellis_private.mutate_before_chat_models(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_context_checkpoints(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
COMMIT;
