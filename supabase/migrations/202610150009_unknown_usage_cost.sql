-- A cumulative usage update can make a previously priced call unpriceable.
BEGIN;
ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_unknown_usage_cost;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_result jsonb; v_call trellis.model_calls%ROWTYPE;
BEGIN
    IF p_action='update_model_call' AND p_payload->'clear_estimated_cost' IS NOT NULL
       AND p_payload->'clear_estimated_cost'<>'null'::jsonb
       AND jsonb_typeof(p_payload->'clear_estimated_cost')<>'boolean' THEN
        RAISE EXCEPTION 'invalid cost clearing flag' USING ERRCODE='22023';
    END IF;
    v_result:=trellis_private.mutate_before_unknown_usage_cost(p_action,p_payload,p_capability);
    IF p_action='update_model_call' AND p_payload->'clear_estimated_cost'='true'::jsonb THEN
        UPDATE trellis.model_calls SET estimated_cost=NULL
        WHERE id=(v_result->>'id')::uuid AND user_id=(SELECT auth.uid()) RETURNING * INTO v_call;
        v_result:=to_jsonb(v_call);
    END IF;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_unknown_usage_cost(text,jsonb,text) FROM PUBLIC,anon,authenticated;
COMMIT;
