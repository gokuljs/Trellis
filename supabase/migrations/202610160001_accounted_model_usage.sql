BEGIN;

ALTER TABLE trellis.model_calls ADD COLUMN accounted_total_tokens bigint
    CHECK (accounted_total_tokens >= 0);

-- Native compaction may contain incomplete iterations. Keep those totals
-- unknown; ordinary calls can use their provider's recorded usage semantics.
UPDATE trellis.model_calls SET accounted_total_tokens =
    input_tokens::bigint + output_tokens::bigint
    + CASE WHEN provider_id = 'anthropic'
        THEN cached_read_tokens::bigint + cache_creation_tokens::bigint ELSE 0 END
WHERE provider_id = adapter_kind AND provider_id IN ('openai', 'anthropic')
    AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL
    AND cached_read_tokens IS NOT NULL AND cache_creation_tokens IS NOT NULL
    AND (reasoning_tokens IS NULL OR reasoning_tokens <= output_tokens)
    AND (provider_id <> 'openai'
        OR cached_read_tokens::bigint + cache_creation_tokens::bigint <= input_tokens)
    AND COALESCE(request_snapshot->>'purpose', '') <> 'native_compaction';

ALTER FUNCTION trellis_private.mutate(text,jsonb,text)
    RENAME TO mutate_before_accounted_model_usage;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_result jsonb;
    v_call trellis.model_calls%ROWTYPE;
    v_count jsonb := p_payload->'accounted_total_tokens';
    v_clear jsonb := p_payload->'clear_accounted_total_tokens';
    v_usage_changed boolean;
BEGIN
    IF p_action='update_model_call' THEN
        IF v_count IS NOT NULL AND v_count<>'null'::jsonb AND
            (jsonb_typeof(v_count)<>'number' OR v_count::text !~ '^[0-9]+$') THEN
            RAISE EXCEPTION 'invalid accounted token count' USING ERRCODE='22023';
        END IF;
        IF v_clear IS NOT NULL AND v_clear<>'null'::jsonb
            AND jsonb_typeof(v_clear)<>'boolean' THEN
            RAISE EXCEPTION 'invalid token clearing flag' USING ERRCODE='22023';
        END IF;
        IF v_clear='true'::jsonb AND v_count IS NOT NULL AND v_count<>'null'::jsonb THEN
            RAISE EXCEPTION 'conflicting accounted token update' USING ERRCODE='22023';
        END IF;
    END IF;
    -- The existing dispatcher locks the run before its call and validates the
    -- lease. Usage, cost, and the normalized total commit in this transaction.
    v_result:=trellis_private.mutate_before_accounted_model_usage(
        p_action,p_payload,p_capability);
    IF p_action='update_model_call' THEN
        v_usage_changed:=EXISTS(SELECT 1 FROM jsonb_each(p_payload) item
            WHERE item.key IN ('input_tokens','output_tokens','reasoning_tokens',
                'cached_read_tokens','cache_creation_tokens') AND item.value<>'null'::jsonb);
        IF v_clear='true'::jsonb OR v_count IS NOT NULL AND v_count<>'null'::jsonb
            OR v_usage_changed THEN
            UPDATE trellis.model_calls SET accounted_total_tokens=
                CASE WHEN v_clear='true'::jsonb THEN NULL ELSE (v_count#>>'{}')::bigint END
            WHERE id=(v_result->>'id')::uuid AND user_id=(SELECT auth.uid())
            RETURNING * INTO v_call;
            v_result:=to_jsonb(v_call);
        END IF;
    END IF;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_accounted_model_usage(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

COMMIT;
