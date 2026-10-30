BEGIN;

CREATE TABLE trellis_private.web_search_months (
    key_hash text NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    month_start date NOT NULL CHECK (extract(day FROM month_start)=1),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 1000),
    PRIMARY KEY(key_hash,month_start)
);
CREATE TABLE trellis_private.web_search_attempts (
    request_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    key_hash text NOT NULL,
    month_start date NOT NULL,
    monthly_limit integer NOT NULL CHECK (monthly_limit BETWEEN 1 AND 1000),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(key_hash,month_start) REFERENCES trellis_private.web_search_months(key_hash,month_start)
);
ALTER TABLE trellis_private.web_search_months ENABLE ROW LEVEL SECURITY;
ALTER TABLE trellis_private.web_search_months FORCE ROW LEVEL SECURITY;
ALTER TABLE trellis_private.web_search_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE trellis_private.web_search_attempts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON trellis_private.web_search_months,trellis_private.web_search_attempts FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis.web_reserve_search_attempt(
    p_payload jsonb,p_request_id uuid,p_key_hash text,p_monthly_limit integer,p_capability text DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_run trellis.runs%ROWTYPE;
    v_res trellis.web_reservations%ROWTYPE;
    v_prior trellis_private.web_search_attempts%ROWTYPE;
    v_month date:=(date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC'))::date;
    v_attempts integer;
BEGIN
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR octet_length(p_payload::text)>8192
       OR p_request_id IS NULL OR p_key_hash IS NULL OR p_key_hash !~ '^[0-9a-f]{64}$'
       OR p_monthly_limit IS NULL OR p_monthly_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
    END IF;
    v_run:=trellis_private.web_scope(p_payload,p_capability);
    IF NOT EXISTS(SELECT 1 FROM trellis.tool_calls WHERE id=(p_payload->>'tool_call_id')::uuid
        AND run_id=v_run.id AND user_id=v_run.user_id AND name='web_search') THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501';
    END IF;
    SELECT * INTO v_res FROM trellis.web_reservations WHERE id=p_request_id AND user_id=v_run.user_id FOR UPDATE;
    IF NOT FOUND OR v_res.run_id<>v_run.id OR v_res.tool_call_id IS DISTINCT FROM (p_payload->>'tool_call_id')::uuid
       OR v_res.dispatch_id IS DISTINCT FROM (p_payload->>'dispatch_id')::uuid
       OR v_res.dispatch_attempt IS DISTINCT FROM (p_payload->>'dispatch_attempt')::integer
       OR v_res.generation<>v_run.generation OR v_res.evidence_epoch IS DISTINCT FROM (p_payload->>'evidence_epoch')::integer
       OR v_res.reserved->>'search_calls' IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501';
    END IF;
    SELECT * INTO v_prior FROM trellis_private.web_search_attempts WHERE request_id=p_request_id;
    IF FOUND THEN
        IF v_prior.user_id<>v_run.user_id OR v_prior.key_hash<>p_key_hash OR v_prior.monthly_limit<>p_monthly_limit THEN
            RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
        END IF;
        RETURN jsonb_build_object('reserved',true);
    END IF;
    IF v_res.status<>'reserved' THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501';
    END IF;
    INSERT INTO trellis_private.web_search_months(key_hash,month_start) VALUES(p_key_hash,v_month) ON CONFLICT DO NOTHING;
    SELECT attempts INTO v_attempts FROM trellis_private.web_search_months
        WHERE key_hash=p_key_hash AND month_start=v_month FOR UPDATE;
    IF v_attempts>=p_monthly_limit THEN
        RAISE EXCEPTION 'web_search_budget_exhausted' USING ERRCODE='22023';
    END IF;
    INSERT INTO trellis_private.web_search_attempts(request_id,user_id,key_hash,month_start,monthly_limit)
        VALUES(p_request_id,v_run.user_id,p_key_hash,v_month,p_monthly_limit);
    UPDATE trellis_private.web_search_months SET attempts=attempts+1 WHERE key_hash=p_key_hash AND month_start=v_month;
    RETURN jsonb_build_object('reserved',true);
END;
$$;
REVOKE ALL ON FUNCTION trellis.web_reserve_search_attempt(jsonb,uuid,text,integer,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.web_reserve_search_attempt(jsonb,uuid,text,integer,text) TO authenticated;

ALTER FUNCTION trellis.web_readiness() RENAME TO web_readiness_before_search;
REVOKE ALL ON FUNCTION trellis.web_readiness_before_search() FROM PUBLIC,anon,authenticated;
CREATE FUNCTION trellis.web_readiness() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('schema_version',2,'ready',
        trellis.web_readiness_before_search()=jsonb_build_object('schema_version',1,'ready',true)
        AND pg_catalog.to_regprocedure('trellis.web_reserve_search_attempt(jsonb,uuid,text,integer,text)') IS NOT NULL
        AND (SELECT count(*)=2 AND bool_and(c.relrowsecurity AND c.relforcerowsecurity)
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='trellis_private' AND c.relname IN ('web_search_months','web_search_attempts'))
        AND NOT pg_catalog.has_table_privilege('authenticated','trellis_private.web_search_months','SELECT')
        AND NOT pg_catalog.has_table_privilege('authenticated','trellis_private.web_search_attempts','SELECT'))
$$;
REVOKE ALL ON FUNCTION trellis.web_readiness() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION trellis.web_readiness() TO anon,authenticated;

COMMIT;
