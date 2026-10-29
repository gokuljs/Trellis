BEGIN;

ALTER TABLE trellis.chats ADD COLUMN web_evidence_epoch integer NOT NULL DEFAULT 0
    CHECK (web_evidence_epoch>=0);
GRANT SELECT (web_evidence_epoch) ON trellis.chats TO authenticated;

CREATE FUNCTION trellis_private.web_usage_valid(p_usage jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path='' AS $$
    SELECT CASE WHEN jsonb_typeof(p_usage)='object' THEN
        (SELECT count(*)=5 AND bool_and(key IN
            ('search_calls','page_attempts','network_requests','response_bytes','extracted_bytes')
            AND jsonb_typeof(value)='number' AND value::text ~ '^[0-9]{1,12}$')
        FROM jsonb_each(p_usage)) ELSE false END
$$;

CREATE FUNCTION trellis_private.web_usage_change(p_current jsonb,p_delta jsonb,p_subtract boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
DECLARE v_result jsonb; v_key text; v_value bigint;
    v_limits jsonb:='{"search_calls":5,"page_attempts":20,"network_requests":300,"response_bytes":67108864,"extracted_bytes":8388608}';
BEGIN
    IF NOT trellis_private.web_usage_valid(p_current) OR NOT trellis_private.web_usage_valid(p_delta) THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
    END IF;
    v_result:=p_current;
    FOR v_key IN SELECT jsonb_object_keys(v_limits) LOOP
        v_value:=(p_current->>v_key)::bigint + (CASE WHEN p_subtract THEN -1 ELSE 1 END)*(p_delta->>v_key)::bigint;
        IF v_value<0 OR v_value>(v_limits->>v_key)::bigint THEN
            RAISE EXCEPTION 'web_budget_exhausted' USING ERRCODE='22023';
        END IF;
        v_result:=jsonb_set(v_result,ARRAY[v_key],to_jsonb(v_value));
    END LOOP;
    RETURN v_result;
END;
$$;

CREATE TABLE trellis.web_accounts (
    user_id uuid PRIMARY KEY REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    retained_bytes bigint NOT NULL DEFAULT 0 CHECK (retained_bytes BETWEEN 0 AND 104857600)
);
CREATE TABLE trellis.web_runs (
    run_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    evidence_epoch integer NOT NULL CHECK (evidence_epoch>=0),
    reservation_count integer NOT NULL DEFAULT 0 CHECK (reservation_count BETWEEN 0 AND 325),
    usage jsonb NOT NULL DEFAULT '{"search_calls":0,"page_attempts":0,"network_requests":0,"response_bytes":0,"extracted_bytes":0}'
        CHECK (trellis_private.web_usage_valid(usage)),
    limits jsonb NOT NULL DEFAULT '{"search_calls":5,"page_attempts":20,"network_requests":300,"response_bytes":67108864,"extracted_bytes":8388608}'
        CHECK (limits='{"search_calls":5,"page_attempts":20,"network_requests":300,"response_bytes":67108864,"extracted_bytes":8388608}'::jsonb),
    UNIQUE(user_id,run_id),
    UNIQUE(user_id,session_id,run_id),
    FOREIGN KEY(user_id,run_id) REFERENCES trellis.runs(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,session_id) REFERENCES trellis.chats(user_id,id) ON DELETE CASCADE
);
CREATE TABLE trellis.web_reservations (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    run_id uuid NOT NULL,
    tool_call_id uuid NOT NULL,
    dispatch_id uuid NOT NULL,
    dispatch_attempt integer NOT NULL CHECK (dispatch_attempt BETWEEN 1 AND 4),
    generation integer NOT NULL CHECK (generation>=0),
    evidence_epoch integer NOT NULL CHECK (evidence_epoch>=0),
    operation_key text NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 200),
    reserved jsonb NOT NULL CHECK (trellis_private.web_usage_valid(reserved)),
    actual jsonb CHECK (actual IS NULL OR trellis_private.web_usage_valid(actual)),
    status text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','settled')),
    CHECK ((status='reserved' AND actual IS NULL) OR (status='settled' AND actual IS NOT NULL)),
    UNIQUE(user_id,id),
    UNIQUE(user_id,run_id,id),
    UNIQUE(user_id,run_id,tool_call_id,dispatch_id,operation_key),
    FOREIGN KEY(user_id,session_id,run_id) REFERENCES trellis.web_runs(user_id,session_id,run_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,run_id,tool_call_id) REFERENCES trellis.tool_calls(user_id,run_id,id) ON DELETE CASCADE
);
CREATE TABLE trellis.web_sources (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    run_id uuid NOT NULL,
    tool_call_id uuid NOT NULL,
    reservation_id uuid NOT NULL UNIQUE,
    evidence_epoch integer NOT NULL CHECK (evidence_epoch>=0),
    requested_url text CHECK (length(requested_url) BETWEEN 1 AND 4096),
    final_url text CHECK (length(final_url) BETWEEN 1 AND 4096),
    canonical_url text CHECK (length(canonical_url) BETWEEN 1 AND 4096),
    title text CHECK (length(title)<=1000),
    publisher text CHECK (length(publisher)<=500),
    published_at text CHECK (length(published_at)<=100),
    fetched_at timestamptz,
    extractor_version text CHECK (length(extractor_version) BETWEEN 1 AND 100),
    content_hash text CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    text text NOT NULL CHECK (octet_length(text)<=262144),
    line_count integer NOT NULL CHECK (line_count>=0),
    byte_count integer NOT NULL CHECK (byte_count BETWEEN 0 AND 262144),
    status text NOT NULL CHECK (status IN ('success','partial','blocked','unavailable','timeout','oversized',
        'unsupported','extraction_failed','cancelled','budget_exhausted','cleared')),
    error_code text CHECK (length(error_code)<=100),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (byte_count=octet_length(text)),
    CHECK (line_count=CASE WHEN text='' THEN 0 ELSE cardinality(string_to_array(text,E'\n')) END),
    CHECK (status NOT IN ('success','partial') OR (byte_count>0 AND final_url IS NOT NULL AND content_hash IS NOT NULL)),
    CHECK (status<>'cleared' OR (text='' AND requested_url IS NULL AND final_url IS NULL AND canonical_url IS NULL
        AND title IS NULL AND publisher IS NULL AND published_at IS NULL AND fetched_at IS NULL
        AND extractor_version IS NULL AND content_hash IS NULL AND error_code IS NULL)),
    UNIQUE(user_id,id),
    UNIQUE(user_id,session_id,id),
    FOREIGN KEY(user_id,session_id,run_id) REFERENCES trellis.web_runs(user_id,session_id,run_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,run_id,reservation_id) REFERENCES trellis.web_reservations(user_id,run_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,run_id,tool_call_id) REFERENCES trellis.tool_calls(user_id,run_id,id) ON DELETE CASCADE
);
CREATE INDEX web_sources_conversation ON trellis.web_sources(user_id,session_id,created_at,id);
CREATE TABLE trellis.web_crawls (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    run_id uuid NOT NULL,
    tool_call_id uuid NOT NULL,
    evidence_epoch integer NOT NULL CHECK (evidence_epoch>=0),
    seed_url text CHECK (length(seed_url) BETWEEN 1 AND 4096),
    origin text CHECK (length(origin) BETWEEN 1 AND 4096),
    path_prefix text CHECK (length(path_prefix) BETWEEN 1 AND 4096),
    max_pages integer NOT NULL CHECK (max_pages BETWEEN 1 AND 20),
    max_depth integer NOT NULL CHECK (max_depth BETWEEN 0 AND 2),
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','stopped','cleared')),
    stop_reason text CHECK (length(stop_reason)<=100),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id,id),
    UNIQUE(user_id,run_id,tool_call_id),
    FOREIGN KEY(user_id,session_id,run_id) REFERENCES trellis.web_runs(user_id,session_id,run_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,run_id,tool_call_id) REFERENCES trellis.tool_calls(user_id,run_id,id) ON DELETE CASCADE
);
CREATE TABLE trellis.web_crawl_pages (
    crawl_id uuid NOT NULL,
    user_id uuid NOT NULL,
    url text NOT NULL CHECK (length(url) BETWEEN 1 AND 4096),
    depth integer NOT NULL CHECK (depth BETWEEN 0 AND 2),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','fetching','completed')),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND 4),
    generation integer NOT NULL DEFAULT 0 CHECK (generation>=0),
    source_id uuid,
    PRIMARY KEY(crawl_id,url),
    FOREIGN KEY(user_id,crawl_id) REFERENCES trellis.web_crawls(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,source_id) REFERENCES trellis.web_sources(user_id,id)
);
CREATE TABLE trellis.web_citations (
    user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    run_id uuid NOT NULL,
    model_call_id uuid NOT NULL,
    source_id uuid NOT NULL,
    start_line integer NOT NULL CHECK (start_line>0),
    end_line integer NOT NULL CHECK (end_line>=start_line),
    citation_index integer NOT NULL CHECK (citation_index BETWEEN 1 AND 50),
    PRIMARY KEY(model_call_id,citation_index),
    FOREIGN KEY(user_id,run_id,model_call_id) REFERENCES trellis.model_calls(user_id,run_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,session_id,source_id) REFERENCES trellis.web_sources(user_id,session_id,id) ON DELETE CASCADE
);
CREATE TABLE trellis.web_citation_sets (
    user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    run_id uuid NOT NULL,
    model_call_id uuid PRIMARY KEY,
    bindings jsonb NOT NULL CHECK (jsonb_typeof(bindings)='array' AND jsonb_array_length(bindings)<=50),
    FOREIGN KEY(user_id,session_id) REFERENCES trellis.chats(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,run_id,model_call_id) REFERENCES trellis.model_calls(user_id,run_id,id) ON DELETE CASCADE
);

DO $$
DECLARE v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY['web_accounts','web_runs','web_reservations','web_sources','web_crawls','web_crawl_pages','web_citations','web_citation_sets'] LOOP
        EXECUTE format('ALTER TABLE trellis.%I ENABLE ROW LEVEL SECURITY',v_table);
        EXECUTE format('ALTER TABLE trellis.%I FORCE ROW LEVEL SECURITY',v_table);
        EXECUTE format('CREATE POLICY owner_read ON trellis.%I FOR SELECT TO authenticated USING (user_id=(SELECT auth.uid()))',v_table);
        EXECUTE format('REVOKE ALL ON trellis.%I FROM PUBLIC,anon,authenticated',v_table);
        EXECUTE format('GRANT SELECT ON trellis.%I TO authenticated',v_table);
    END LOOP;
END;
$$;

CREATE FUNCTION trellis_private.web_source_metadata(p_source trellis.web_sources) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path='' AS $$
    SELECT to_jsonb(p_source)-'text'-'reservation_id'
$$;
CREATE FUNCTION trellis_private.web_url_in_scope(p_url text,p_origin text,p_prefix text) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path='' AS $$
    SELECT COALESCE(left(p_url,length(p_origin)+1)=p_origin||'/' AND
        (p_prefix='/' OR split_part(split_part(substr(p_url,length(p_origin)+1),'?',1),'#',1)=rtrim(p_prefix,'/')
        OR left(substr(p_url,length(p_origin)+1),length(rtrim(p_prefix,'/'))+1)=rtrim(p_prefix,'/')||'/'),false)
$$;
CREATE FUNCTION trellis_private.web_scope(p_payload jsonb,p_capability text,p_open boolean DEFAULT false)
RETURNS trellis.runs LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_tool trellis.tool_calls%ROWTYPE; v_epoch integer;
BEGIN
    IF p_payload->>'user_id' IS DISTINCT FROM v_user::text THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501';
    END IF;
    INSERT INTO trellis.web_accounts(user_id) VALUES(v_user) ON CONFLICT DO NOTHING;
    PERFORM 1 FROM trellis.web_accounts WHERE user_id=v_user FOR UPDATE;
    v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
    IF v_run.chat_id IS DISTINCT FROM (p_payload->>'session_id')::uuid
       OR v_run.generation IS DISTINCT FROM (p_payload->>'generation')::integer
       OR v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL
       OR v_run.deadline_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501';
    END IF;
    SELECT web_evidence_epoch INTO v_epoch FROM trellis.chats
        WHERE user_id=v_user AND id=v_run.chat_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501'; END IF;
    SELECT * INTO v_tool FROM trellis.tool_calls
        WHERE user_id=v_user AND run_id=v_run.id AND id=(p_payload->>'tool_call_id')::uuid FOR UPDATE;
    IF NOT FOUND OR v_tool.status<>'running' OR v_tool.name NOT IN ('web_search','web_read','web_crawl','web_source')
       OR v_tool.recovery_policy IS DISTINCT FROM 'checkpointed'
       OR v_tool.dispatch_id IS DISTINCT FROM (p_payload->>'dispatch_id')::uuid
       OR v_tool.dispatch_attempt IS DISTINCT FROM (p_payload->>'dispatch_attempt')::integer
       OR v_tool.dispatch_generation IS DISTINCT FROM v_run.generation
       OR (NOT p_open AND v_epoch IS DISTINCT FROM (p_payload->>'evidence_epoch')::integer) THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501';
    END IF;
    RETURN v_run;
END;
$$;

CREATE FUNCTION trellis_private.web_mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE; v_web trellis.web_runs%ROWTYPE;
    v_res trellis.web_reservations%ROWTYPE; v_source trellis.web_sources%ROWTYPE; v_crawl trellis.web_crawls%ROWTYPE;
    v_page trellis.web_crawl_pages%ROWTYPE; v_chat uuid; v_epoch integer; v_count integer; v_limit integer;
    v_offset integer; v_total integer; v_start integer; v_max integer; v_bytes bigint; v_text text;
    v_item jsonb; v_result jsonb; v_usage jsonb; v_refund jsonb; v_key text; v_actual bigint;
    v_lines text[]; v_items jsonb; v_call uuid; v_index integer; v_existing jsonb;
BEGIN
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR octet_length(p_payload::text)>1048576 THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
    END IF;
    IF p_action IN ('list_sources','read_source','clear_sources','get_crawl','list_pages','read_citations') THEN
        v_chat:=(p_payload->>'session_id')::uuid;
        IF p_action='clear_sources' THEN
            INSERT INTO trellis.web_accounts(user_id) VALUES(v_user) ON CONFLICT DO NOTHING;
            PERFORM 1 FROM trellis.web_accounts WHERE user_id=v_user FOR UPDATE;
        END IF;
        SELECT web_evidence_epoch INTO v_epoch FROM trellis.chats WHERE id=v_chat AND user_id=v_user FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF p_action='clear_sources' THEN
            IF EXISTS(SELECT 1 FROM trellis.runs WHERE user_id=v_user AND chat_id=v_chat
                AND status IN ('queued','running','recovering','waiting_for_approval','cancelling')) THEN
                RAISE EXCEPTION 'web_sources_busy' USING ERRCODE='22023';
            END IF;
            SELECT COALESCE(sum(byte_count),0),count(*) FILTER(WHERE status<>'cleared')
                INTO v_bytes,v_count FROM trellis.web_sources WHERE user_id=v_user AND session_id=v_chat;
            UPDATE trellis.web_accounts SET retained_bytes=retained_bytes-v_bytes WHERE user_id=v_user;
            UPDATE trellis.web_sources SET requested_url=NULL,final_url=NULL,canonical_url=NULL,title=NULL,publisher=NULL,
                published_at=NULL,fetched_at=NULL,extractor_version=NULL,content_hash=NULL,text='',line_count=0,byte_count=0,
                status='cleared',error_code=NULL WHERE user_id=v_user AND session_id=v_chat AND status<>'cleared';
            DELETE FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id IN
                (SELECT id FROM trellis.web_crawls WHERE user_id=v_user AND session_id=v_chat);
            UPDATE trellis.web_crawls SET seed_url=NULL,origin=NULL,path_prefix=NULL,status='cleared',stop_reason=NULL,
                updated_at=clock_timestamp() WHERE user_id=v_user AND session_id=v_chat;
            UPDATE trellis.web_reservations SET operation_key='cleared:'||id::text
                WHERE user_id=v_user AND session_id=v_chat;
            UPDATE trellis.chats SET web_evidence_epoch=web_evidence_epoch+1 WHERE id=v_chat AND user_id=v_user
                RETURNING web_evidence_epoch INTO v_epoch;
            RETURN jsonb_build_object('evidence_epoch',v_epoch,'cleared_count',v_count);
        ELSIF p_action='read_source' THEN
            v_start:=(p_payload->>'start_line')::integer; v_max:=(p_payload->>'max_lines')::integer;
            IF v_start IS NULL OR v_start NOT BETWEEN 1 AND 262145 OR v_max IS NULL OR v_max NOT BETWEEN 1 AND 200 THEN
                RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
            END IF;
            SELECT * INTO v_source FROM trellis.web_sources
                WHERE user_id=v_user AND session_id=v_chat AND id=(p_payload->>'source_id')::uuid;
            IF NOT FOUND THEN RETURN NULL; END IF;
            v_lines:=CASE WHEN v_source.text='' THEN ARRAY[]::text[] ELSE string_to_array(v_source.text,E'\n') END;
            v_items:='[]'::jsonb; v_bytes:=0;
            FOR v_index IN v_start..least(v_source.line_count,v_start+v_max-1) LOOP
                IF v_bytes+octet_length(v_lines[v_index])>20000 THEN EXIT; END IF;
                v_items:=v_items||jsonb_build_array(v_lines[v_index]);
                v_bytes:=v_bytes+octet_length(v_lines[v_index]);
            END LOOP;
            RETURN jsonb_build_object('source',trellis_private.web_source_metadata(v_source),'start_line',v_start,
                'lines',v_items,'total_lines',v_source.line_count);
        ELSIF p_action='list_sources' THEN
            v_offset:=(p_payload->>'offset')::integer; v_limit:=(p_payload->>'limit')::integer;
            IF v_offset IS NULL OR v_offset NOT BETWEEN 0 AND 1000000 OR v_limit IS NULL OR v_limit NOT BETWEEN 1 AND 50 THEN
                RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
            END IF;
            SELECT count(*) INTO v_total FROM trellis.web_sources WHERE user_id=v_user AND session_id=v_chat;
            SELECT COALESCE(jsonb_agg(trellis_private.web_source_metadata(s) ORDER BY s.created_at,s.id),'[]'::jsonb)
                INTO v_items FROM (SELECT * FROM trellis.web_sources WHERE user_id=v_user AND session_id=v_chat
                    ORDER BY created_at,id OFFSET v_offset LIMIT v_limit) s;
            RETURN jsonb_build_object('items',v_items,'next_offset',CASE WHEN v_offset+v_limit<v_total THEN v_offset+v_limit END);
        ELSIF p_action='read_citations' THEN
            SELECT COALESCE(jsonb_agg(jsonb_build_object('model_call_id',model_call_id,'source_id',source_id,
                'start_line',start_line,'end_line',end_line,'citation_index',citation_index) ORDER BY citation_index),'[]'::jsonb)
                INTO v_items FROM trellis.web_citations WHERE user_id=v_user AND session_id=v_chat
                AND model_call_id=(p_payload->>'model_call_id')::uuid;
            RETURN v_items;
        END IF;
        SELECT * INTO v_crawl FROM trellis.web_crawls WHERE user_id=v_user AND session_id=v_chat
            AND id=(p_payload->>'crawl_id')::uuid;
        IF NOT FOUND OR v_crawl.status='cleared' THEN RETURN NULL; END IF;
        IF p_action='get_crawl' THEN RETURN to_jsonb(v_crawl); END IF;
        v_offset:=(p_payload->>'offset')::integer; v_limit:=(p_payload->>'limit')::integer;
        IF v_offset IS NULL OR v_offset NOT BETWEEN 0 AND 1000000 OR v_limit IS NULL OR v_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
        END IF;
        SELECT count(*) INTO v_total FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id;
        SELECT COALESCE(jsonb_agg(to_jsonb(p)-'user_id' ORDER BY p.depth,p.url),'[]'::jsonb) INTO v_items
            FROM (SELECT * FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id
                ORDER BY depth,url OFFSET v_offset LIMIT v_limit) p;
        RETURN jsonb_build_object('items',v_items,'next_offset',CASE WHEN v_offset+v_limit<v_total THEN v_offset+v_limit END);
    END IF;

    IF p_action='bind_citations' THEN
        v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        v_call:=(p_payload->>'model_call_id')::uuid;
        IF v_run.generation IS DISTINCT FROM (p_payload->>'generation')::integer
           OR v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL OR v_run.deadline_at<=clock_timestamp()
           OR NOT EXISTS(SELECT 1 FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id AND id=v_call)
           OR jsonb_typeof(p_payload->'citations') IS DISTINCT FROM 'array' OR jsonb_array_length(p_payload->'citations')>50 THEN
            RAISE EXCEPTION 'citation_invalid' USING ERRCODE='22023';
        END IF;
        v_index:=0; v_items:='[]'::jsonb;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_payload->'citations') LOOP
            v_index:=v_index+1;
            SELECT * INTO v_source FROM trellis.web_sources WHERE user_id=v_user AND session_id=v_run.chat_id
                AND id=(v_item->>'source_id')::uuid AND status IN ('success','partial');
            IF NOT FOUND OR (v_item->>'start_line')::integer IS NULL OR (v_item->>'start_line')::integer<1
               OR (v_item->>'end_line')::integer IS NULL OR (v_item->>'end_line')::integer<(v_item->>'start_line')::integer
               OR (v_item->>'end_line')::integer>v_source.line_count
               OR (v_item->>'end_line')::integer-(v_item->>'start_line')::integer>=200 THEN
                RAISE EXCEPTION 'citation_invalid' USING ERRCODE='22023';
            END IF;
            v_items:=v_items||jsonb_build_array(jsonb_build_object('model_call_id',v_call,'source_id',v_source.id,
                'start_line',(v_item->>'start_line')::integer,'end_line',(v_item->>'end_line')::integer,'citation_index',v_index));
        END LOOP;
        SELECT bindings INTO v_existing FROM trellis.web_citation_sets WHERE user_id=v_user AND model_call_id=v_call;
        IF FOUND THEN
            IF v_existing IS DISTINCT FROM v_items THEN RAISE EXCEPTION 'citation_invalid' USING ERRCODE='22023'; END IF;
            RETURN v_existing;
        END IF;
        INSERT INTO trellis.web_citation_sets(user_id,session_id,run_id,model_call_id,bindings)
            VALUES(v_user,v_run.chat_id,v_run.id,v_call,v_items);
        FOR v_item IN SELECT value FROM jsonb_array_elements(v_items) LOOP
            INSERT INTO trellis.web_citations(user_id,session_id,run_id,model_call_id,source_id,start_line,end_line,citation_index)
            VALUES(v_user,v_run.chat_id,v_run.id,v_call,(v_item->>'source_id')::uuid,(v_item->>'start_line')::integer,
                (v_item->>'end_line')::integer,(v_item->>'citation_index')::integer);
        END LOOP;
        RETURN v_items;
    END IF;

    v_run:=trellis_private.web_scope(p_payload,p_capability,p_action='open_run');
    SELECT web_evidence_epoch INTO v_epoch FROM trellis.chats WHERE user_id=v_user AND id=v_run.chat_id;
    IF p_action='open_run' THEN
        INSERT INTO trellis.web_runs(run_id,user_id,session_id,evidence_epoch)
            VALUES(v_run.id,v_user,v_run.chat_id,v_epoch) ON CONFLICT DO NOTHING;
    END IF;
    SELECT * INTO v_web FROM trellis.web_runs WHERE user_id=v_user AND run_id=v_run.id FOR UPDATE;
    IF NOT FOUND OR v_web.evidence_epoch<>v_epoch THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501'; END IF;
    IF p_action='open_run' THEN RETURN jsonb_build_object('evidence_epoch',v_epoch,'usage',v_web.usage); END IF;

    IF p_action='reserve' THEN
        v_usage:=p_payload->'usage';
        IF NOT trellis_private.web_usage_valid(v_usage) OR p_payload->>'operation_key' IS NULL
           OR length(p_payload->>'operation_key') NOT BETWEEN 1 AND 200
           OR p_payload->>'reservation_id' IS NULL THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023'; END IF;
        IF (v_usage->>'search_calls')::bigint+(v_usage->>'page_attempts')::bigint+(v_usage->>'network_requests')::bigint=0 THEN
            RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
        END IF;
        SELECT * INTO v_res FROM trellis.web_reservations WHERE user_id=v_user AND run_id=v_run.id
            AND tool_call_id=(p_payload->>'tool_call_id')::uuid AND dispatch_id=(p_payload->>'dispatch_id')::uuid
            AND operation_key=p_payload->>'operation_key';
        IF FOUND THEN
            IF v_res.id IS DISTINCT FROM (p_payload->>'reservation_id')::uuid OR v_res.reserved IS DISTINCT FROM v_usage THEN
                RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
            END IF;
            RETURN to_jsonb(v_res);
        END IF;
        IF v_web.reservation_count>=325 THEN RAISE EXCEPTION 'web_budget_exhausted' USING ERRCODE='22023'; END IF;
        v_usage:=trellis_private.web_usage_change(v_web.usage,v_usage);
        INSERT INTO trellis.web_reservations(id,user_id,session_id,run_id,tool_call_id,dispatch_id,dispatch_attempt,generation,
            evidence_epoch,operation_key,reserved) VALUES((p_payload->>'reservation_id')::uuid,v_user,v_run.chat_id,v_run.id,
            (p_payload->>'tool_call_id')::uuid,(p_payload->>'dispatch_id')::uuid,(p_payload->>'dispatch_attempt')::integer,
            v_run.generation,v_epoch,p_payload->>'operation_key',p_payload->'usage') RETURNING * INTO v_res;
        UPDATE trellis.web_runs SET usage=v_usage,reservation_count=reservation_count+1 WHERE run_id=v_run.id AND user_id=v_user;
        RETURN to_jsonb(v_res);
    ELSIF p_action IN ('settle','record_source') THEN
        SELECT * INTO v_res FROM trellis.web_reservations WHERE user_id=v_user AND run_id=v_run.id
            AND id=(p_payload->>'reservation_id')::uuid FOR UPDATE;
        IF NOT FOUND OR v_res.tool_call_id IS DISTINCT FROM (p_payload->>'tool_call_id')::uuid
           OR v_res.dispatch_id IS DISTINCT FROM (p_payload->>'dispatch_id')::uuid OR v_res.generation<>v_run.generation
           OR v_res.dispatch_attempt IS DISTINCT FROM (p_payload->>'dispatch_attempt')::integer
           OR v_res.evidence_epoch<>v_epoch THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501'; END IF;
        IF p_action='settle' THEN
            v_usage:=p_payload->'usage';
            IF NOT trellis_private.web_usage_valid(v_usage) THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023'; END IF;
            IF v_res.status='settled' THEN
                IF v_res.actual IS DISTINCT FROM v_usage THEN RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023'; END IF;
                RETURN to_jsonb(v_res);
            END IF;
            v_refund:='{}'::jsonb;
            FOR v_key IN SELECT jsonb_object_keys(v_res.reserved) LOOP
                v_actual:=(v_usage->>v_key)::bigint;
                IF v_actual>(v_res.reserved->>v_key)::bigint THEN RAISE EXCEPTION 'web_budget_exhausted' USING ERRCODE='22023'; END IF;
                v_refund:=jsonb_set(v_refund,ARRAY[v_key],to_jsonb((v_res.reserved->>v_key)::bigint-v_actual));
            END LOOP;
            SELECT * INTO v_source FROM trellis.web_sources WHERE user_id=v_user AND reservation_id=v_res.id;
            IF (v_usage->>'extracted_bytes')::bigint<>COALESCE(v_source.byte_count,0) THEN
                RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
            END IF;
            IF v_source.id IS NOT NULL AND (v_usage->>'page_attempts')::bigint<1 THEN
                RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
            END IF;
            UPDATE trellis.web_runs SET usage=trellis_private.web_usage_change(usage,v_refund,true)
                WHERE user_id=v_user AND run_id=v_run.id;
            UPDATE trellis.web_reservations SET actual=v_usage,status='settled' WHERE user_id=v_user AND id=v_res.id RETURNING * INTO v_res;
            RETURN to_jsonb(v_res);
        END IF;
        v_item:=p_payload->'source'; v_text:=v_item->>'text';
        IF jsonb_typeof(v_item) IS DISTINCT FROM 'object' OR jsonb_typeof(v_item->'text') IS DISTINCT FROM 'string'
           OR EXISTS(SELECT 1 FROM unnest(ARRAY['id','requested_url','fetched_at','extractor_version','status']) k
                WHERE jsonb_typeof(v_item->k) IS DISTINCT FROM 'string')
           OR v_text IS NULL OR octet_length(v_text)>262144 OR v_item->>'status' IS NULL
           OR v_item->>'status' NOT IN ('success','partial','blocked','unavailable','timeout','oversized','unsupported',
                'extraction_failed','cancelled','budget_exhausted')
           OR (v_item->>'status' NOT IN ('success','partial') AND v_text<>'')
           OR EXISTS(SELECT 1 FROM unnest(string_to_array(v_text,E'\n')) line WHERE length(line)>2048)
           OR v_item->>'id' IS NULL OR v_item->>'requested_url' IS NULL OR v_item->>'fetched_at' IS NULL
           OR length(v_item->>'fetched_at') NOT BETWEEN 1 AND 100
           OR length(v_item->>'requested_url') NOT BETWEEN 1 AND 4096
           OR v_item->>'extractor_version' IS NULL OR length(v_item->>'extractor_version') NOT BETWEEN 1 AND 100
           OR EXISTS(SELECT 1 FROM jsonb_each(v_item) m WHERE m.key IN
                ('final_url','canonical_url','title','publisher','published_at','error_code')
                AND (jsonb_typeof(m.value) NOT IN ('string','null') OR m.value='""'::jsonb))
           OR v_text IS DISTINCT FROM replace(replace(v_text,E'\r\n',E'\n'),E'\r',E'\n') THEN
            RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
        END IF;
        SELECT * INTO v_source FROM trellis.web_sources WHERE user_id=v_user AND reservation_id=v_res.id;
        IF FOUND THEN
            IF v_source.id IS DISTINCT FROM (v_item->>'id')::uuid OR v_source.text IS DISTINCT FROM v_text
               OR v_source.requested_url IS DISTINCT FROM v_item->>'requested_url'
               OR v_source.status IS DISTINCT FROM v_item->>'status' OR v_source.final_url IS DISTINCT FROM v_item->>'final_url'
               OR v_source.canonical_url IS DISTINCT FROM v_item->>'canonical_url' OR v_source.title IS DISTINCT FROM v_item->>'title'
               OR v_source.publisher IS DISTINCT FROM v_item->>'publisher' OR v_source.published_at IS DISTINCT FROM v_item->>'published_at'
               OR v_source.fetched_at IS DISTINCT FROM (v_item->>'fetched_at')::timestamptz
               OR v_source.extractor_version IS DISTINCT FROM v_item->>'extractor_version'
               OR v_source.error_code IS DISTINCT FROM v_item->>'error_code' THEN
                RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
            END IF;
            RETURN trellis_private.web_source_metadata(v_source);
        END IF;
        v_bytes:=octet_length(v_text);
        IF v_res.status<>'reserved' OR v_bytes>(v_res.reserved->>'extracted_bytes')::bigint
           OR (v_res.reserved->>'page_attempts')::bigint<1 THEN
            RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
        END IF;
        IF EXISTS(SELECT 1 FROM trellis.web_accounts WHERE user_id=v_user AND retained_bytes+v_bytes>104857600) THEN
            RAISE EXCEPTION 'web_storage_limit' USING ERRCODE='22023';
        END IF;
        INSERT INTO trellis.web_sources(id,user_id,session_id,run_id,tool_call_id,reservation_id,evidence_epoch,
            requested_url,final_url,canonical_url,title,publisher,published_at,fetched_at,extractor_version,content_hash,
            text,line_count,byte_count,status,error_code) VALUES((v_item->>'id')::uuid,v_user,v_run.chat_id,v_run.id,
            v_res.tool_call_id,v_res.id,v_epoch,v_item->>'requested_url',v_item->>'final_url',v_item->>'canonical_url',
            v_item->>'title',v_item->>'publisher',v_item->>'published_at',(v_item->>'fetched_at')::timestamptz,
            v_item->>'extractor_version',encode(sha256(convert_to(v_text,'UTF8')),'hex'),v_text,
            CASE WHEN v_text='' THEN 0 ELSE cardinality(string_to_array(v_text,E'\n')) END,v_bytes,
            v_item->>'status',v_item->>'error_code') RETURNING * INTO v_source;
        UPDATE trellis.web_accounts SET retained_bytes=retained_bytes+v_bytes WHERE user_id=v_user;
        RETURN trellis_private.web_source_metadata(v_source);
    ELSIF p_action='create_crawl' THEN
        IF length(p_payload->>'seed_url') NOT BETWEEN 1 AND 4096 OR length(p_payload->>'origin') NOT BETWEEN 1 AND 4096
           OR p_payload->>'origin' !~ '^https?://[^/?#@]+$' OR left(p_payload->>'path_prefix',1) IS DISTINCT FROM '/'
           OR NOT trellis_private.web_url_in_scope(p_payload->>'seed_url',p_payload->>'origin',p_payload->>'path_prefix')
           OR (p_payload->>'max_pages')::integer NOT BETWEEN 1 AND 20 OR (p_payload->>'max_depth')::integer NOT BETWEEN 0 AND 2 THEN
            RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
        END IF;
        SELECT * INTO v_crawl FROM trellis.web_crawls WHERE user_id=v_user AND id=(p_payload->>'crawl_id')::uuid;
        IF FOUND THEN
            IF v_crawl.run_id<>v_run.id OR v_crawl.tool_call_id IS DISTINCT FROM (p_payload->>'tool_call_id')::uuid
               OR v_crawl.seed_url IS DISTINCT FROM p_payload->>'seed_url' OR v_crawl.origin IS DISTINCT FROM p_payload->>'origin'
               OR v_crawl.path_prefix IS DISTINCT FROM p_payload->>'path_prefix' OR v_crawl.max_pages IS DISTINCT FROM (p_payload->>'max_pages')::integer
               OR v_crawl.max_depth IS DISTINCT FROM (p_payload->>'max_depth')::integer THEN
                RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
            END IF;
            RETURN to_jsonb(v_crawl);
        END IF;
        IF EXISTS(SELECT 1 FROM trellis.web_crawls WHERE user_id=v_user AND run_id=v_run.id
            AND tool_call_id=(p_payload->>'tool_call_id')::uuid) THEN
            RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
        END IF;
        INSERT INTO trellis.web_crawls(id,user_id,session_id,run_id,tool_call_id,evidence_epoch,seed_url,origin,path_prefix,max_pages,max_depth)
            VALUES((p_payload->>'crawl_id')::uuid,v_user,v_run.chat_id,v_run.id,(p_payload->>'tool_call_id')::uuid,v_epoch,
                p_payload->>'seed_url',p_payload->>'origin',p_payload->>'path_prefix',(p_payload->>'max_pages')::integer,
                (p_payload->>'max_depth')::integer) RETURNING * INTO v_crawl;
        RETURN to_jsonb(v_crawl);
    END IF;
    SELECT * INTO v_crawl FROM trellis.web_crawls WHERE user_id=v_user AND run_id=v_run.id
        AND tool_call_id=(p_payload->>'tool_call_id')::uuid AND id=(p_payload->>'crawl_id')::uuid FOR UPDATE;
    IF NOT FOUND OR v_crawl.evidence_epoch<>v_epoch THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='42501'; END IF;
    IF p_action='checkpoint_crawl' THEN
        IF p_payload->>'status' NOT IN ('running','completed','stopped') OR length(p_payload->>'stop_reason')>100 THEN
            RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
        END IF;
        IF v_crawl.status='completed' AND p_payload->>'status'<>'completed' THEN
            RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.web_crawls SET status=p_payload->>'status',stop_reason=p_payload->>'stop_reason',updated_at=clock_timestamp()
            WHERE user_id=v_user AND id=v_crawl.id RETURNING * INTO v_crawl;
        RETURN to_jsonb(v_crawl);
    END IF;
    IF v_crawl.status<>'running' THEN RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023'; END IF;
    IF p_action='enqueue_pages' THEN
        IF jsonb_typeof(p_payload->'pages') IS DISTINCT FROM 'array' OR jsonb_array_length(p_payload->'pages')>200 THEN
            RAISE EXCEPTION 'web_frontier_limit' USING ERRCODE='22023';
        END IF;
        v_count:=0;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_payload->'pages') LOOP
            v_text:=v_item->>'url'; v_index:=(v_item->>'depth')::integer;
            IF v_text IS NULL OR length(v_text)>4096 OR v_index IS NULL OR v_index NOT BETWEEN 0 AND v_crawl.max_depth
               OR NOT trellis_private.web_url_in_scope(v_text,v_crawl.origin,v_crawl.path_prefix) THEN
                RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
            END IF;
            INSERT INTO trellis.web_crawl_pages(crawl_id,user_id,url,depth) VALUES(v_crawl.id,v_user,v_text,v_index)
                ON CONFLICT DO NOTHING;
            IF FOUND THEN v_count:=v_count+1; END IF;
        END LOOP;
        IF (SELECT count(*) FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id)>200 THEN
            RAISE EXCEPTION 'web_frontier_limit' USING ERRCODE='22023';
        END IF;
        RETURN jsonb_build_object('enqueued_count',v_count);
    ELSIF p_action='claim_page' THEN
        IF (SELECT count(*) FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id AND attempt>0)>=v_crawl.max_pages THEN
            SELECT * INTO v_page FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id
                AND status='fetching' AND generation<v_run.generation AND attempt<4 ORDER BY depth,url LIMIT 1;
        ELSE
            SELECT * INTO v_page FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id
                AND (status='queued' OR (status='fetching' AND generation<v_run.generation AND attempt<4)) ORDER BY depth,url LIMIT 1;
        END IF;
        IF NOT FOUND THEN RETURN NULL; END IF;
        UPDATE trellis.web_crawl_pages SET status='fetching',attempt=attempt+1,generation=v_run.generation
            WHERE crawl_id=v_crawl.id AND url=v_page.url RETURNING * INTO v_page;
        RETURN to_jsonb(v_page)-'user_id';
    ELSIF p_action='complete_page' THEN
        SELECT * INTO v_page FROM trellis.web_crawl_pages WHERE user_id=v_user AND crawl_id=v_crawl.id AND url=p_payload->>'url';
        IF NOT FOUND OR (v_page.status='completed' AND v_page.source_id IS DISTINCT FROM (p_payload->>'source_id')::uuid)
           OR (v_page.status<>'completed' AND (v_page.status<>'fetching' OR v_page.generation<>v_run.generation)) THEN
            RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
        END IF;
        IF NOT EXISTS(SELECT 1 FROM trellis.web_sources WHERE user_id=v_user AND session_id=v_run.chat_id
            AND run_id=v_run.id AND tool_call_id=v_crawl.tool_call_id AND id=(p_payload->>'source_id')::uuid
            AND requested_url=v_page.url AND evidence_epoch=v_epoch) THEN
            RAISE EXCEPTION 'web_source_conflict' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.web_crawl_pages SET status='completed',source_id=(p_payload->>'source_id')::uuid
            WHERE crawl_id=v_crawl.id AND url=v_page.url RETURNING * INTO v_page;
        RETURN to_jsonb(v_page)-'user_id';
    END IF;
    RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
END;
$$;

CREATE FUNCTION trellis.web_mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT trellis_private.web_mutate(p_action,p_payload,p_capability)
$$;
REVOKE ALL ON FUNCTION trellis_private.web_usage_valid(jsonb),trellis_private.web_usage_change(jsonb,jsonb,boolean),
    trellis_private.web_source_metadata(trellis.web_sources),trellis_private.web_scope(jsonb,text,boolean),
    trellis_private.web_url_in_scope(text,text,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
GRANT EXECUTE ON FUNCTION trellis_private.web_usage_valid(jsonb) TO authenticated;
REVOKE ALL ON FUNCTION trellis_private.web_mutate(text,jsonb,text),trellis.web_mutate(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
GRANT EXECUTE ON FUNCTION trellis_private.web_mutate(text,jsonb,text),trellis.web_mutate(text,jsonb,text) TO authenticated;

COMMIT;
