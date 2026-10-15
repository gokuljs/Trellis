BEGIN;
ALTER TABLE trellis.chats ADD COLUMN context_checkpoint_revision integer NOT NULL DEFAULT 0
    CHECK (context_checkpoint_revision>=0);
CREATE TABLE trellis.context_checkpoints (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    chat_id uuid NOT NULL,
    run_id uuid NOT NULL,
    model_call_id uuid NOT NULL,
    revision integer NOT NULL CHECK(revision>0),
    parent_id uuid,
    provider_id text NOT NULL,
    model_id text NOT NULL,
    adapter_kind text NOT NULL,
    sources jsonb NOT NULL CHECK(jsonb_typeof(sources)='array' AND jsonb_array_length(sources) BETWEEN 1 AND 512
        AND octet_length(sources::text)<=131072),
    summary text NOT NULL CHECK(btrim(summary)<>'' AND octet_length(summary)<=32768),
    source_hash text NOT NULL CHECK(source_hash ~ '^[a-f0-9]{64}$'),
    input_hash text NOT NULL CHECK(input_hash ~ '^[a-f0-9]{64}$'),
    instruction_hash text NOT NULL CHECK(instruction_hash ~ '^[a-f0-9]{64}$'),
    workspace_hash text NOT NULL CHECK(workspace_hash ~ '^[a-f0-9]{64}$'),
    input_tokens_before integer NOT NULL CHECK(input_tokens_before>=0),
    input_tokens_after integer NOT NULL CHECK(input_tokens_after>=0),
    pricing_context_tokens integer NOT NULL DEFAULT 0 CHECK(pricing_context_tokens>=0),
    covered_message_ordinal integer NOT NULL CHECK(covered_message_ordinal>=0),
    covered_run_message_ordinal integer NOT NULL CHECK(covered_run_message_ordinal>=0),
    native_kind text CHECK(native_kind IN ('openai_window','anthropic_block')),
    native_payload_json text CHECK(octet_length(native_payload_json)<=262144),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK((native_kind IS NULL)=(native_payload_json IS NULL)),
    UNIQUE(user_id,chat_id,id), UNIQUE(user_id,chat_id,revision),
    FOREIGN KEY(user_id,chat_id) REFERENCES trellis.chats(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,run_id) REFERENCES trellis.runs(user_id,id),
    FOREIGN KEY(user_id,model_call_id) REFERENCES trellis.model_calls(user_id,id),
    FOREIGN KEY(user_id,chat_id,parent_id) REFERENCES trellis.context_checkpoints(user_id,chat_id,id),
    FOREIGN KEY(provider_id,model_id) REFERENCES trellis.models(provider_id,id)
);
ALTER TABLE trellis.context_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE trellis.context_checkpoints FORCE ROW LEVEL SECURITY;
REVOKE ALL ON trellis.context_checkpoints FROM PUBLIC,anon,authenticated;
CREATE POLICY context_checkpoints_read ON trellis.context_checkpoints FOR SELECT TO authenticated
    USING(user_id=(SELECT auth.uid()));
GRANT SELECT ON trellis.context_checkpoints TO authenticated;
CREATE FUNCTION trellis_private.reject_checkpoint_update() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN RAISE EXCEPTION 'context checkpoints are immutable' USING ERRCODE='42501'; END;
$$;
REVOKE ALL ON FUNCTION trellis_private.reject_checkpoint_update() FROM PUBLIC,anon,authenticated;
CREATE TRIGGER checkpoint_immutable BEFORE UPDATE ON trellis.context_checkpoints
    FOR EACH ROW EXECUTE FUNCTION trellis_private.reject_checkpoint_update();
-- One statement snapshot prevents a new checkpoint from moving the tail cursor
-- between two reads. RLS applies to both checkpoint and transcript rows.
CREATE FUNCTION trellis.read_context_window(p_session_id uuid,p_limit integer DEFAULT 128)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE v_result jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'context page limit is invalid' USING ERRCODE='22023';
    END IF;
    WITH checkpoint AS MATERIALIZED (
        SELECT * FROM trellis.context_checkpoints WHERE user_id=(SELECT auth.uid())
            AND chat_id=p_session_id ORDER BY revision DESC LIMIT 1
    ), tail AS (
        SELECT * FROM trellis.messages WHERE user_id=(SELECT auth.uid()) AND chat_id=p_session_id
            AND ordinal>COALESCE((SELECT covered_message_ordinal FROM checkpoint),0)
            ORDER BY ordinal ASC LIMIT p_limit
    )
    SELECT jsonb_build_object('checkpoint',(SELECT to_jsonb(c) FROM checkpoint c),
        'messages',COALESCE((SELECT jsonb_agg(to_jsonb(t) ORDER BY ordinal) FROM tail t),'[]'::jsonb))
        INTO v_result;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis.read_context_window(uuid,integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis.read_context_window(uuid,integer) TO authenticated;
ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_context_checkpoints;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := auth.uid(); v_run trellis.runs%ROWTYPE; v_call trellis.model_calls%ROWTYPE;
    v_chat trellis.chats%ROWTYPE; v_parent trellis.context_checkpoints%ROWTYPE;
    v_existing trellis.context_checkpoints%ROWTYPE; v_saved trellis.context_checkpoints%ROWTYPE;
    v_message trellis.messages%ROWTYPE; v_run_message trellis.run_messages%ROWTYPE;
    v_id uuid; v_expected integer; v_sources jsonb; v_ref jsonb; v_source_id uuid; v_kind text;
    v_before integer; v_covered integer; v_before_run integer; v_covered_run integer;
    v_messages integer:=0; v_run_messages integer:=0; v_parents integer:=0;
    v_seen text[]:='{}'::text[]; v_native jsonb;
BEGIN
    IF p_action<>'save_context_checkpoint' THEN
        RETURN trellis_private.mutate_before_context_checkpoints(p_action,p_payload,p_capability);
    END IF;
    IF v_user IS NULL THEN RAISE EXCEPTION 'authentication required' USING ERRCODE='42501'; END IF;
    v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
    IF v_run.status<>'running' THEN RAISE EXCEPTION 'checkpoint run is not running' USING ERRCODE='22023'; END IF;
    SELECT * INTO v_call FROM trellis.model_calls WHERE id=(p_payload->>'model_call_id')::uuid
        AND user_id=v_user AND run_id=v_run.id AND status='completed';
    IF NOT FOUND OR COALESCE(v_call.request_snapshot->>'purpose','') NOT IN ('summary','native_compaction') THEN
        RAISE EXCEPTION 'checkpoint requires a completed compaction call' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_chat FROM trellis.chats WHERE id=v_run.chat_id AND user_id=v_user FOR UPDATE;
    v_id:=(p_payload->>'id')::uuid; v_expected:=(p_payload->>'expected_revision')::integer;
    IF v_id IS NULL OR v_expected IS NULL OR v_expected<0 THEN
        RAISE EXCEPTION 'invalid checkpoint identity or revision' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_existing FROM trellis.context_checkpoints WHERE id=v_id AND user_id=v_user;
    IF FOUND THEN
        IF v_existing.run_id=v_run.id AND v_existing.model_call_id=v_call.id AND v_existing.revision=v_expected+1
           AND v_existing.sources IS NOT DISTINCT FROM p_payload->'sources'
           AND v_existing.summary IS NOT DISTINCT FROM p_payload->>'summary'
           AND v_existing.source_hash IS NOT DISTINCT FROM p_payload->>'source_hash'
           AND v_existing.input_hash IS NOT DISTINCT FROM p_payload->>'input_hash'
           AND v_existing.instruction_hash IS NOT DISTINCT FROM p_payload->>'instruction_hash'
           AND v_existing.workspace_hash IS NOT DISTINCT FROM p_payload->>'workspace_hash'
           AND v_existing.input_tokens_before=(p_payload->>'input_tokens_before')::integer
           AND v_existing.input_tokens_after=(p_payload->>'input_tokens_after')::integer
           AND v_existing.pricing_context_tokens>=COALESCE((p_payload->>'pricing_context_tokens')::integer,0)
           AND v_existing.native_kind IS NOT DISTINCT FROM p_payload->>'native_kind'
           AND v_existing.native_payload_json IS NOT DISTINCT FROM p_payload->>'native_payload_json' THEN
            RETURN to_jsonb(v_existing);
        END IF;
        RAISE EXCEPTION 'checkpoint id conflicts with saved content' USING ERRCODE='22023';
    END IF;
    IF v_chat.context_checkpoint_revision<>v_expected THEN
        RAISE EXCEPTION 'checkpoint revision conflict' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_parent FROM trellis.context_checkpoints WHERE user_id=v_user AND chat_id=v_chat.id
        ORDER BY revision DESC LIMIT 1;
    v_before:=COALESCE(v_parent.covered_message_ordinal,0); v_covered:=v_before;
    v_before_run:=CASE WHEN v_parent.run_id=v_run.id THEN v_parent.covered_run_message_ordinal ELSE 0 END;
    v_covered_run:=v_before_run;
    v_sources:=p_payload->'sources';
    IF v_sources IS NULL OR jsonb_typeof(v_sources)<>'array' OR jsonb_array_length(v_sources) NOT BETWEEN 1 AND 512
       OR octet_length(v_sources::text)>131072 OR COALESCE(btrim(p_payload->>'summary'),'')=''
       OR octet_length(p_payload->>'summary')>32768 THEN
        RAISE EXCEPTION 'invalid checkpoint source or summary bounds' USING ERRCODE='22023';
    END IF;
    FOR v_ref IN SELECT value FROM jsonb_array_elements(v_sources) LOOP
        v_source_id:=(v_ref->>'source_id')::uuid; v_kind:=v_ref->>'source_kind';
        IF v_source_id IS NULL OR COALESCE(length(v_ref->>'turn_id'),0) NOT BETWEEN 1 AND 200
           OR v_kind||':'||v_source_id::text=ANY(v_seen) THEN
            RAISE EXCEPTION 'invalid checkpoint source identity' USING ERRCODE='22023';
        END IF;
        v_seen:=array_append(v_seen,v_kind||':'||v_source_id::text);
        IF v_kind='checkpoint' THEN
            IF v_parent.id IS NULL OR v_source_id<>v_parent.id THEN
                RAISE EXCEPTION 'checkpoint prefix must include its current parent' USING ERRCODE='22023';
            END IF;
            v_parents:=v_parents+1;
        ELSIF v_kind='message' THEN
            SELECT * INTO v_message FROM trellis.messages WHERE id=v_source_id AND user_id=v_user
                AND chat_id=v_chat.id AND turn_id=v_ref->>'turn_id';
            IF NOT FOUND OR v_message.ordinal<=v_before THEN
                RAISE EXCEPTION 'checkpoint cannot rehydrate archived transcript' USING ERRCODE='22023';
            END IF;
            IF v_message.id=v_run.input_message_id THEN
                RAISE EXCEPTION 'checkpoint cannot cover the current request' USING ERRCODE='22023';
            END IF;
            v_messages:=v_messages+1; v_covered:=greatest(v_covered,v_message.ordinal);
        ELSIF v_kind='run_message' THEN
            SELECT * INTO v_run_message FROM trellis.run_messages WHERE id=v_source_id AND user_id=v_user
                AND run_id=v_run.id;
            IF NOT FOUND OR v_ref->>'turn_id' IS DISTINCT FROM v_run.turn_id OR v_run_message.ordinal<=v_before_run THEN
                RAISE EXCEPTION 'checkpoint source does not belong to run' USING ERRCODE='22023';
            END IF;
            v_run_messages:=v_run_messages+1; v_covered_run:=greatest(v_covered_run,v_run_message.ordinal);
        ELSE RAISE EXCEPTION 'invalid checkpoint source kind' USING ERRCODE='22023'; END IF;
    END LOOP;
    IF v_parents<>(CASE WHEN v_parent.id IS NULL THEN 0 ELSE 1 END) OR
       v_messages<>(SELECT count(*) FROM trellis.messages WHERE user_id=v_user AND chat_id=v_chat.id
           AND ordinal>v_before AND ordinal<=v_covered) OR
       v_run_messages<>(SELECT count(*) FROM trellis.run_messages WHERE user_id=v_user AND run_id=v_run.id
           AND ordinal>v_before_run AND ordinal<=v_covered_run) THEN
        RAISE EXCEPTION 'checkpoint must cover a contiguous unarchived prefix' USING ERRCODE='22023';
    END IF;
    IF v_messages>0 AND EXISTS(SELECT 1 FROM trellis.messages m WHERE m.user_id=v_user AND m.chat_id=v_chat.id
        AND m.ordinal>v_covered AND m.turn_id=(SELECT turn_id FROM trellis.messages
            WHERE user_id=v_user AND chat_id=v_chat.id AND ordinal=v_covered)) THEN
        RAISE EXCEPTION 'checkpoint cannot split a conversation turn' USING ERRCODE='22023';
    END IF;
    IF EXISTS(SELECT 1 FROM trellis.tool_calls t JOIN trellis.run_messages a ON a.id=t.assistant_message_id
        WHERE t.user_id=v_user AND t.run_id=v_run.id AND a.ordinal>v_before_run AND a.ordinal<=v_covered_run
        AND NOT EXISTS(SELECT 1 FROM trellis.run_messages r WHERE r.user_id=v_user AND r.run_id=v_run.id
            AND r.tool_call_id=t.id AND r.ordinal<=v_covered_run)) THEN
        RAISE EXCEPTION 'checkpoint cannot split a tool exchange' USING ERRCODE='22023';
    END IF;
    IF p_payload->>'native_kind' IS NOT NULL THEN
        IF octet_length(p_payload->>'native_payload_json')>262144 THEN
            RAISE EXCEPTION 'native checkpoint exceeds size limit' USING ERRCODE='22023'; END IF;
        v_native:=(p_payload->>'native_payload_json')::jsonb;
        IF NOT ((p_payload->>'native_kind'='openai_window' AND v_run.adapter_kind='openai'
                    AND jsonb_typeof(v_native)='array' AND jsonb_array_length(v_native)>0)
            OR (p_payload->>'native_kind'='anthropic_block' AND v_run.adapter_kind='anthropic'
                    AND jsonb_typeof(v_native)='object' AND v_native->>'type'='compaction')) THEN
            RAISE EXCEPTION 'native checkpoint does not match model adapter' USING ERRCODE='22023'; END IF;
    END IF;
    INSERT INTO trellis.context_checkpoints(id,user_id,chat_id,run_id,model_call_id,revision,parent_id,
        provider_id,model_id,adapter_kind,sources,summary,source_hash,input_hash,instruction_hash,workspace_hash,
        input_tokens_before,input_tokens_after,pricing_context_tokens,covered_message_ordinal,
        covered_run_message_ordinal,native_kind,native_payload_json)
    VALUES(v_id,v_user,v_chat.id,v_run.id,v_call.id,v_expected+1,v_parent.id,v_run.provider_id,v_run.model_id,
        v_run.adapter_kind,v_sources,p_payload->>'summary',p_payload->>'source_hash',p_payload->>'input_hash',
        p_payload->>'instruction_hash',p_payload->>'workspace_hash',(p_payload->>'input_tokens_before')::integer,
        (p_payload->>'input_tokens_after')::integer,greatest(COALESCE((p_payload->>'pricing_context_tokens')::integer,0),
        COALESCE(v_parent.pricing_context_tokens,0)),v_covered,v_covered_run,p_payload->>'native_kind',p_payload->>'native_payload_json')
    RETURNING * INTO v_saved;
    UPDATE trellis.chats SET context_checkpoint_revision=v_expected+1 WHERE id=v_chat.id AND user_id=v_user;
    RETURN to_jsonb(v_saved);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_context_checkpoints(text,jsonb,text) FROM PUBLIC,anon,authenticated;
COMMIT;
