BEGIN;

ALTER TABLE trellis.web_crawl_pages
    ADD COLUMN discovered_links jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(discovered_links)='array' AND jsonb_array_length(discovered_links)<=32),
    ADD COLUMN finalization_hash text CHECK (finalization_hash ~ '^[0-9a-f]{64}$'),
    ADD COLUMN claimed_dispatch_id uuid;
ALTER TABLE trellis.web_crawl_pages ALTER CONSTRAINT web_crawl_pages_user_id_source_id_fkey
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE trellis_private.web_crawl_origins (
    origin_hash text PRIMARY KEY CHECK (origin_hash ~ '^[0-9a-f]{64}$'),
    next_allowed_at timestamptz NOT NULL,
    last_attempt_key text CHECK (last_attempt_key ~ '^[0-9a-f]{64}$')
);
CREATE TABLE trellis_private.web_crawl_origin_attempts (
    crawl_id uuid NOT NULL,
    url text NOT NULL,
    page_attempt integer NOT NULL CHECK (page_attempt BETWEEN 1 AND 4),
    origin_hash text NOT NULL REFERENCES trellis_private.web_crawl_origins(origin_hash),
    granted_at timestamptz NOT NULL,
    PRIMARY KEY(crawl_id,url,page_attempt),
    FOREIGN KEY(crawl_id,url) REFERENCES trellis.web_crawl_pages(crawl_id,url) ON DELETE CASCADE
);
ALTER TABLE trellis_private.web_crawl_origins ENABLE ROW LEVEL SECURITY;
ALTER TABLE trellis_private.web_crawl_origins FORCE ROW LEVEL SECURITY;
ALTER TABLE trellis_private.web_crawl_origin_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE trellis_private.web_crawl_origin_attempts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON trellis_private.web_crawl_origins,trellis_private.web_crawl_origin_attempts FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis_private.web_crawl_path(p_path text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
DECLARE v_path text:=p_path; v_decoded text; v_bytes bytea; v_index integer; v_round integer; v_char text;
BEGIN
    IF v_path IS NULL OR length(v_path) NOT BETWEEN 1 AND 4096 OR left(v_path,1)<>'/' THEN RETURN NULL; END IF;
    FOR v_round IN 1..4 LOOP
        IF position('%' IN v_path)=0 THEN EXIT; END IF;
        v_bytes:=''::bytea; v_index:=1;
        WHILE v_index<=length(v_path) LOOP
            v_char:=substr(v_path,v_index,1);
            IF v_char='%' THEN
                IF substr(v_path,v_index+1,2) !~ '^[0-9A-Fa-f]{2}$' THEN RETURN NULL; END IF;
                v_bytes:=v_bytes||decode(substr(v_path,v_index+1,2),'hex');
                v_index:=v_index+3;
            ELSE
                v_bytes:=v_bytes||convert_to(v_char,'UTF8');
                v_index:=v_index+1;
            END IF;
        END LOOP;
        v_decoded:=convert_from(v_bytes,'UTF8');
        IF v_decoded=v_path THEN EXIT; END IF;
        v_path:=v_decoded;
    END LOOP;
    IF position('%' IN v_path)>0 OR position(E'\\' IN v_path)>0 OR v_path ~ '[[:cntrl:]]'
        OR EXISTS(SELECT 1 FROM unnest(string_to_array(v_path,'/')) p WHERE p IN ('.','..')) THEN RETURN NULL; END IF;
    RETURN v_path;
EXCEPTION WHEN character_not_in_repertoire OR untranslatable_character THEN RETURN NULL;
END;
$$;

CREATE FUNCTION trellis_private.web_crawl_url_in_scope(p_url text,p_origin text,p_prefix text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
DECLARE v_path text; v_prefix text; v_authority text; v_host text;
BEGIN
    IF p_url IS NULL OR p_origin IS NULL OR p_prefix IS NULL OR length(p_url) NOT BETWEEN 1 AND 4096
        OR p_url ~ '[[:space:][:cntrl:]]' OR position(E'\\' IN p_url)>0 OR position('#' IN p_url)>0
        OR p_origin !~ '^https?://([a-z0-9]([a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:]+\])(:80|:443)?$'
        OR left(p_url,length(p_origin)+1)<>p_origin||'/' THEN RETURN false; END IF;
    IF p_origin ~ '^http://.*:80$' OR p_origin ~ '^https://.*:443$' THEN RETURN false; END IF;
    v_authority:=split_part(p_origin,'://',2);
    IF left(v_authority,1)='[' THEN
        v_host:=substring(v_authority FROM '^\[([^]]+)\]');
        IF host(v_host::inet)<>v_host THEN RETURN false; END IF;
    ELSE
        v_host:=split_part(v_authority,':',1);
        IF EXISTS(SELECT 1 FROM unnest(string_to_array(v_host,'.')) label
            WHERE label !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$') THEN RETURN false; END IF;
        IF v_host ~ '^[0-9.]+$' AND host(v_host::inet)<>v_host THEN RETURN false; END IF;
    END IF;
    v_path:=trellis_private.web_crawl_path(split_part(substr(p_url,length(p_origin)+1),'?',1));
    v_prefix:=trellis_private.web_crawl_path(p_prefix);
    IF v_path IS NULL OR v_prefix IS NULL THEN RETURN false; END IF;
    v_prefix:=rtrim(v_prefix,'/');
    RETURN v_prefix='' OR v_path=v_prefix OR left(v_path,length(v_prefix)+1)=v_prefix||'/';
EXCEPTION WHEN invalid_text_representation THEN RETURN false;
END;
$$;

CREATE OR REPLACE FUNCTION trellis_private.web_url_in_scope(p_url text,p_origin text,p_prefix text) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path='' AS $$
    SELECT trellis_private.web_crawl_url_in_scope(p_url,p_origin,p_prefix)
$$;

ALTER FUNCTION trellis_private.web_mutate(text,jsonb,text) RENAME TO web_mutate_before_crawl;
REVOKE ALL ON FUNCTION trellis_private.web_mutate_before_crawl(text,jsonb,text) FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis_private.web_mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_user uuid; v_run trellis.runs%ROWTYPE; v_crawl trellis.web_crawls%ROWTYPE;
    v_page trellis.web_crawl_pages%ROWTYPE; v_item jsonb; v_link text; v_links jsonb;
    v_hash text; v_origin_hash text; v_attempt_key text; v_last_attempt_key text;
    v_granted timestamptz; v_next timestamptz; v_now timestamptz;
    v_count integer; v_attempt integer; v_result jsonb;
BEGIN
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR octet_length(p_payload::text)>1048576 THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
    END IF;
    IF p_action='create_crawl' AND NOT trellis_private.web_crawl_url_in_scope(
        p_payload->>'seed_url',p_payload->>'origin',p_payload->>'path_prefix') THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
    END IF;
    IF p_action NOT IN ('finalize_crawl_page','reserve_crawl_origin','enqueue_pages','claim_page') THEN
        RETURN trellis_private.web_mutate_before_crawl(p_action,p_payload,p_capability);
    END IF;
    v_user:=trellis_private.require_user();
    v_run:=trellis_private.web_scope(p_payload,p_capability);
    PERFORM 1 FROM trellis.web_runs WHERE user_id=v_user AND run_id=v_run.id
        AND evidence_epoch=(p_payload->>'evidence_epoch')::integer FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501'; END IF;
    SELECT * INTO v_crawl FROM trellis.web_crawls WHERE user_id=v_user AND run_id=v_run.id
        AND tool_call_id=(p_payload->>'tool_call_id')::uuid AND id=(p_payload->>'crawl_id')::uuid
        AND evidence_epoch=(p_payload->>'evidence_epoch')::integer FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501'; END IF;
    IF p_action='enqueue_pages' THEN
        IF jsonb_typeof(p_payload->'pages') IS DISTINCT FROM 'array' OR jsonb_array_length(p_payload->'pages')>200 THEN
            RAISE EXCEPTION 'web_frontier_limit' USING ERRCODE='22023';
        END IF;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_payload->'pages') LOOP
            IF NOT trellis_private.web_crawl_url_in_scope(v_item->>'url',v_crawl.origin,v_crawl.path_prefix) THEN
                RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
            END IF;
        END LOOP;
        RETURN trellis_private.web_mutate_before_crawl(p_action,p_payload,p_capability);
    END IF;
    IF p_action='claim_page' THEN
        IF v_crawl.status<>'running' THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023'; END IF;
        SELECT count(*) INTO v_count FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id AND attempt>0;
        SELECT * INTO v_page FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id
            AND ((status='queued' AND v_count<v_crawl.max_pages) OR (status='fetching' AND attempt<4
                AND (generation<v_run.generation OR claimed_dispatch_id IS DISTINCT FROM (p_payload->>'dispatch_id')::uuid)))
            ORDER BY depth,url LIMIT 1;
        IF NOT FOUND THEN RETURN NULL; END IF;
        UPDATE trellis.web_crawl_pages SET status='fetching',attempt=attempt+1,generation=v_run.generation,
            claimed_dispatch_id=(p_payload->>'dispatch_id')::uuid WHERE crawl_id=v_crawl.id AND url=v_page.url RETURNING * INTO v_page;
        RETURN to_jsonb(v_page)-'user_id'-'finalization_hash';
    END IF;
    SELECT * INTO v_page FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id
        AND url=p_payload->>'url' FOR UPDATE;
    v_attempt:=(p_payload->>'page_attempt')::integer;
    IF NOT FOUND OR v_attempt IS NULL OR v_attempt NOT BETWEEN 1 AND 4 OR v_page.attempt<>v_attempt THEN
        RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
    END IF;
    IF p_action='reserve_crawl_origin' THEN
        IF v_crawl.status<>'running' OR v_page.status<>'fetching' OR v_page.generation<>v_run.generation
            OR v_page.claimed_dispatch_id IS DISTINCT FROM (p_payload->>'dispatch_id')::uuid THEN
            RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501';
        END IF;
        v_origin_hash:=encode(sha256(convert_to(v_crawl.origin,'UTF8')),'hex');
        v_attempt_key:=encode(sha256(convert_to(jsonb_build_array(v_crawl.id,v_page.url,v_attempt)::text,'UTF8')),'hex');
        v_now:=clock_timestamp();
        INSERT INTO trellis_private.web_crawl_origins(origin_hash,next_allowed_at) VALUES(v_origin_hash,v_now)
            ON CONFLICT DO NOTHING;
        SELECT next_allowed_at,last_attempt_key INTO v_next,v_last_attempt_key
            FROM trellis_private.web_crawl_origins WHERE origin_hash=v_origin_hash FOR UPDATE;
        v_now:=clock_timestamp();
        SELECT granted_at INTO v_granted FROM trellis_private.web_crawl_origin_attempts
            WHERE crawl_id=v_crawl.id AND url=v_page.url AND page_attempt=v_attempt;
        IF FOUND AND v_last_attempt_key=v_attempt_key AND v_granted+interval '1 second'>v_now THEN
            RETURN jsonb_build_object('wait_seconds',0);
        END IF;
        IF v_next>v_now THEN
            RETURN jsonb_build_object('wait_seconds',extract(epoch FROM v_next-v_now));
        END IF;
        INSERT INTO trellis_private.web_crawl_origin_attempts(crawl_id,url,page_attempt,origin_hash,granted_at)
            VALUES(v_crawl.id,v_page.url,v_attempt,v_origin_hash,v_now)
            ON CONFLICT(crawl_id,url,page_attempt) DO UPDATE SET granted_at=EXCLUDED.granted_at;
        UPDATE trellis_private.web_crawl_origins SET next_allowed_at=v_now+interval '1 second',last_attempt_key=v_attempt_key
            WHERE origin_hash=v_origin_hash;
        RETURN jsonb_build_object('wait_seconds',0);
    END IF;
    v_links:=p_payload->'discovered_links';
    IF jsonb_typeof(v_links) IS DISTINCT FROM 'array' OR jsonb_array_length(v_links)>32 THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
    END IF;
    FOR v_item IN SELECT value FROM jsonb_array_elements(v_links) LOOP
        IF jsonb_typeof(v_item) IS DISTINCT FROM 'string'
            OR NOT trellis_private.web_crawl_url_in_scope(v_item#>>'{}',v_crawl.origin,v_crawl.path_prefix) THEN
            RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
        END IF;
    END LOOP;
    v_hash:=encode(sha256(convert_to(jsonb_build_object('crawl_id',v_crawl.id,'url',v_page.url,'page_attempt',v_attempt,
        'reservation_id',p_payload->'reservation_id','source',p_payload->'source','usage',p_payload->'usage',
        'discovered_links',v_links)::text,'UTF8')),'hex');
    IF v_page.status='completed' THEN
        IF v_page.finalization_hash IS DISTINCT FROM v_hash THEN
            RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
        END IF;
        RETURN to_jsonb(v_page)-'user_id'-'finalization_hash';
    END IF;
    IF v_crawl.status<>'running' OR v_page.status<>'fetching' OR v_page.generation<>v_run.generation
        OR v_page.claimed_dispatch_id IS DISTINCT FROM (p_payload->>'dispatch_id')::uuid
        OR p_payload->'source'->>'requested_url' IS DISTINCT FROM v_page.url THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501';
    END IF;
    PERFORM trellis_private.web_mutate_before_crawl('record_source',p_payload,p_capability);
    PERFORM trellis_private.web_mutate_before_crawl('settle',p_payload,p_capability);
    UPDATE trellis.web_crawl_pages SET status='completed',source_id=(p_payload->'source'->>'id')::uuid,
        discovered_links=v_links,finalization_hash=v_hash WHERE crawl_id=v_crawl.id AND url=v_page.url RETURNING * INTO v_page;
    IF v_page.depth<v_crawl.max_depth THEN
        SELECT count(*) INTO v_count FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl.id;
        FOR v_link IN SELECT DISTINCT value#>>'{}' FROM jsonb_array_elements(v_links) ORDER BY 1 LOOP
            IF EXISTS(SELECT 1 FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl.id AND url=v_link) THEN CONTINUE; END IF;
            IF v_count>=200 THEN
                UPDATE trellis.web_crawls SET status='stopped',stop_reason='frontier_limit',updated_at=clock_timestamp()
                    WHERE user_id=v_user AND id=v_crawl.id;
                EXIT;
            END IF;
            INSERT INTO trellis.web_crawl_pages(crawl_id,user_id,url,depth) VALUES(v_crawl.id,v_user,v_link,v_page.depth+1)
                ON CONFLICT DO NOTHING;
            IF FOUND THEN v_count:=v_count+1; END IF;
        END LOOP;
    END IF;
    RETURN to_jsonb(v_page)-'user_id'-'finalization_hash';
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.web_crawl_path(text),trellis_private.web_crawl_url_in_scope(text,text,text),
    trellis_private.web_mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.web_mutate(text,jsonb,text) TO authenticated;

ALTER FUNCTION trellis.web_readiness() RENAME TO web_readiness_before_crawl;
REVOKE ALL ON FUNCTION trellis.web_readiness_before_crawl() FROM PUBLIC,anon,authenticated;
CREATE FUNCTION trellis.web_readiness() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('schema_version',3,'ready',
        trellis.web_readiness_before_crawl()=jsonb_build_object('schema_version',2,'ready',true)
        AND pg_catalog.to_regprocedure('trellis_private.web_crawl_url_in_scope(text,text,text)') IS NOT NULL
        AND (SELECT count(*)=3 FROM information_schema.columns WHERE table_schema='trellis'
            AND table_name='web_crawl_pages' AND column_name IN ('discovered_links','finalization_hash','claimed_dispatch_id'))
        AND (SELECT count(*)=2 AND bool_and(c.relrowsecurity AND c.relforcerowsecurity)
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='trellis_private' AND c.relname IN ('web_crawl_origins','web_crawl_origin_attempts'))
        AND NOT pg_catalog.has_table_privilege('authenticated','trellis_private.web_crawl_origins','SELECT')
        AND NOT pg_catalog.has_table_privilege('authenticated','trellis_private.web_crawl_origin_attempts','SELECT'))
$$;
REVOKE ALL ON FUNCTION trellis.web_readiness() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION trellis.web_readiness() TO anon,authenticated;

COMMIT;
