BEGIN;

-- The operator sets a password out of band. This role has no table grants,
-- account/RPC access, or role memberships; it can execute this one function.
DO $role$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trellis_catalog_sync') THEN
        CREATE ROLE trellis_catalog_sync LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trellis_catalog_sync' AND
        (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR rolinherit))
       OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles
            WHERE rolname='trellis_catalog_sync')) THEN
        RAISE EXCEPTION 'existing catalog role has unsafe privileges' USING ERRCODE='42501';
    END IF;
END;
$role$;
REVOKE ALL ON SCHEMA trellis FROM trellis_catalog_sync;
REVOKE ALL ON ALL TABLES IN SCHEMA trellis FROM trellis_catalog_sync;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA trellis FROM trellis_catalog_sync;
REVOKE ALL ON SCHEMA trellis_private FROM trellis_catalog_sync;
REVOKE ALL ON ALL TABLES IN SCHEMA trellis_private FROM trellis_catalog_sync;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA trellis_private FROM trellis_catalog_sync;
GRANT USAGE ON SCHEMA trellis_private TO trellis_catalog_sync;

CREATE FUNCTION trellis_private.sync_model_catalog(p_provider text,p_available_ids jsonb)
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
    UPDATE trellis.models SET availability='unavailable',availability_checked_at=v_now,enabled=false
    WHERE provider_id=p_provider AND NOT (p_available_ids ? upstream_model_id);
    -- Pending discoveries cannot be selected. Remove absent rows unless an old
    -- account/run references one, preserving all historical foreign keys.
    DELETE FROM trellis.models m WHERE m.provider_id=p_provider AND m.metadata_status='pending'
        AND m.availability='unavailable'
        AND NOT EXISTS(SELECT 1 FROM trellis.user_settings s WHERE s.selected_model_id=m.id)
        AND NOT EXISTS(SELECT 1 FROM trellis.runs r WHERE r.model_id=m.id)
        AND NOT EXISTS(SELECT 1 FROM trellis.model_calls c WHERE c.model_id=m.id)
        -- The additive chat preference column arrives in the next migration.
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
-- Account access and conversation reads survive an empty availability list.
-- Existing settings remain unchanged; new accounts get a verified default that
-- is displayed as unavailable until discovery recovers, never a pending model.
CREATE OR REPLACE FUNCTION trellis_private.ensure_account() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_model trellis.models%ROWTYPE;
BEGIN
    SELECT * INTO v_model FROM trellis.models WHERE metadata_status='ready'
        ORDER BY enabled DESC,catalog_order LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'verified model catalog empty' USING ERRCODE='55000';
    END IF;
    INSERT INTO trellis.profiles(id) VALUES(v_user) ON CONFLICT DO NOTHING;
    INSERT INTO trellis.user_settings(user_id,selected_provider,selected_model_id)
        VALUES(v_user,v_model.provider_id,v_model.id) ON CONFLICT DO NOTHING;
    INSERT INTO trellis.onboarding_progress(user_id) VALUES(v_user) ON CONFLICT DO NOTHING;
    RETURN jsonb_build_object('user_id',v_user);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.ensure_account() FROM PUBLIC,anon,trellis_catalog_sync;
GRANT EXECUTE ON FUNCTION trellis_private.ensure_account() TO authenticated;
COMMIT;
