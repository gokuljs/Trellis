BEGIN;

ALTER FUNCTION trellis_private.web_mutate(text,jsonb,text) RENAME TO web_mutate_before_research_crawl;
REVOKE ALL ON FUNCTION trellis_private.web_mutate_before_research_crawl(text,jsonb,text) FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis_private.web_mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_previous text; v_result jsonb;
BEGIN
    IF p_action IN ('reserve_crawl_origin','finalize_crawl_page') THEN
        v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        v_previous:=current_setting('trellis.web_limits',true);
        PERFORM set_config('trellis.web_limits',trellis_private.research_web_limits(v_run)::text,true);
        v_result:=trellis_private.web_mutate_before_research_crawl(p_action,p_payload,p_capability);
        PERFORM set_config('trellis.web_limits',COALESCE(v_previous,''),true);
        RETURN v_result;
    END IF;
    RETURN trellis_private.web_mutate_before_research_crawl(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.web_mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.web_mutate(text,jsonb,text) TO authenticated;

COMMIT;
