BEGIN;

CREATE TABLE trellis.run_assistant_outputs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    chat_id uuid NOT NULL,
    turn_id text NOT NULL,
    run_id uuid NOT NULL,
    model_call_id uuid NOT NULL,
    step_index integer NOT NULL CHECK (step_index > 0),
    content text NOT NULL DEFAULT '' CHECK (octet_length(content) <= 256000),
    status text NOT NULL DEFAULT 'streaming' CHECK (status IN ('streaming','complete','partial')),
    next_batch_index integer NOT NULL DEFAULT 0 CHECK (next_batch_index >= 0),
    stop_reason text,
    promoted_message_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id,id), UNIQUE (model_call_id),
    FOREIGN KEY (user_id,chat_id) REFERENCES trellis.chats(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY (user_id,run_id) REFERENCES trellis.runs(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY (user_id,model_call_id) REFERENCES trellis.model_calls(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY (user_id,promoted_message_id) REFERENCES trellis.messages(user_id,id)
);
CREATE INDEX assistant_outputs_chat_order ON trellis.run_assistant_outputs(user_id,chat_id,created_at,id);
ALTER TABLE trellis.run_assistant_outputs ENABLE ROW LEVEL SECURITY;
CREATE POLICY assistant_outputs_read ON trellis.run_assistant_outputs
    FOR SELECT TO authenticated USING (user_id = (SELECT auth.uid()));
GRANT SELECT ON trellis.run_assistant_outputs TO authenticated;
REVOKE INSERT,UPDATE,DELETE ON trellis.run_assistant_outputs FROM anon,authenticated;

CREATE FUNCTION trellis_private.finalize_assistant_outputs() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    UPDATE trellis.run_assistant_outputs SET status='partial',updated_at=now(),
        stop_reason=COALESCE(NEW.stop_reason,NEW.status)
    WHERE run_id=NEW.id AND user_id=NEW.user_id AND status='streaming';
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.finalize_assistant_outputs() FROM PUBLIC,anon,authenticated;
CREATE TRIGGER finalize_assistant_outputs AFTER UPDATE OF status ON trellis.runs
FOR EACH ROW WHEN (NEW.status IN ('completed','failed','cancelled','interrupted'))
EXECUTE FUNCTION trellis_private.finalize_assistant_outputs();

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_outputs;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := auth.uid();
    v_run trellis.runs%ROWTYPE;
    v_call trellis.model_calls%ROWTYPE;
    v_output trellis.run_assistant_outputs%ROWTYPE;
    v_batch integer;
    v_text text;
    v_event jsonb;
    v_result jsonb;
BEGIN
    IF v_user IS NULL THEN RAISE EXCEPTION 'authentication required' USING ERRCODE='42501'; END IF;
    IF p_action IN ('append_assistant_batch','finish_assistant_output','complete_run') THEN
        v_run := trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        IF v_run.status <> 'running' THEN
            RAISE EXCEPTION 'run is not running' USING ERRCODE='22023';
        END IF;
        IF p_action IN ('append_assistant_batch','finish_assistant_output') THEN
            SELECT * INTO v_call FROM trellis.model_calls
            WHERE id=(p_payload->>'model_call_id')::uuid AND run_id=v_run.id AND user_id=v_user;
            IF NOT FOUND THEN RAISE EXCEPTION 'model call does not belong to run' USING ERRCODE='22023'; END IF;
            SELECT * INTO v_output FROM trellis.run_assistant_outputs
            WHERE model_call_id=v_call.id AND user_id=v_user FOR UPDATE;
        ELSE
            SELECT * INTO v_output FROM trellis.run_assistant_outputs
            WHERE run_id=v_run.id AND user_id=v_user ORDER BY step_index DESC LIMIT 1 FOR UPDATE;
        END IF;
    END IF;

    IF p_action='append_assistant_batch' THEN
        v_batch := (p_payload->>'batch_index')::integer;
        v_text := p_payload->>'text';
        IF v_batch IS NULL OR v_batch < 0 OR v_text IS NULL OR v_text=''
           OR octet_length(v_text)>4096 OR v_call.status <> 'streaming' THEN
            RAISE EXCEPTION 'invalid assistant batch' USING ERRCODE='22023';
        END IF;
        IF v_output.id IS NOT NULL AND v_batch < v_output.next_batch_index THEN
            SELECT to_jsonb(e) INTO v_event FROM trellis.run_events e
            WHERE user_id=v_user AND run_id=v_run.id AND event_type='assistant.delta'
              AND data->>'model_call_id'=v_call.id::text
              AND (data->>'batch_index')::integer=v_batch;
            IF v_event IS NULL OR v_event->'data'->>'text' IS DISTINCT FROM v_text THEN
                RAISE EXCEPTION 'assistant batch conflicts with saved text' USING ERRCODE='22023';
            END IF;
            RETURN jsonb_build_object('output',to_jsonb(v_output),'event',v_event);
        END IF;
        IF v_batch <> COALESCE(v_output.next_batch_index,0) THEN
            RAISE EXCEPTION 'assistant batches must be in order' USING ERRCODE='22023';
        END IF;
        IF v_output.id IS NULL THEN
            INSERT INTO trellis.run_assistant_outputs(user_id,chat_id,turn_id,run_id,model_call_id,step_index)
            VALUES(v_user,v_run.chat_id,v_run.turn_id,v_run.id,v_call.id,v_call.step_index)
            RETURNING * INTO v_output;
        END IF;
        IF v_output.status <> 'streaming' THEN RAISE EXCEPTION 'output is not streaming' USING ERRCODE='22023'; END IF;
        UPDATE trellis.run_assistant_outputs SET content=content||v_text,next_batch_index=next_batch_index+1,updated_at=now()
        WHERE id=v_output.id RETURNING * INTO v_output;
        v_event := trellis_private.append_event(v_user,v_run.id,'assistant.delta',jsonb_build_object(
            'output_id',v_output.id,'model_call_id',v_call.id,'step_index',v_call.step_index,
            'batch_index',v_batch,'text',v_text));
        RETURN jsonb_build_object('output',to_jsonb(v_output),'event',v_event);
    ELSIF p_action='finish_assistant_output' THEN
        IF v_output.id IS NULL THEN RETURN NULL; END IF;
        IF v_output.status='complete' AND v_output.stop_reason IS NOT DISTINCT FROM p_payload->>'stop_reason' THEN
            RETURN to_jsonb(v_output);
        END IF;
        IF v_output.status <> 'streaming' THEN RAISE EXCEPTION 'output already finalized' USING ERRCODE='22023'; END IF;
        UPDATE trellis.run_assistant_outputs SET status='complete',stop_reason=p_payload->>'stop_reason',updated_at=now()
        WHERE id=v_output.id RETURNING * INTO v_output;
        RETURN to_jsonb(v_output);
    ELSIF p_action='complete_run' AND v_output.id IS NOT NULL THEN
        v_text := p_payload->>'content';
        IF v_text IS DISTINCT FROM v_output.content THEN
            RAISE EXCEPTION 'completed answer differs from published text' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.run_assistant_outputs SET status='complete',stop_reason='final_response',updated_at=now() WHERE id=v_output.id;
        v_result := trellis_private.mutate_before_outputs(p_action,p_payload,p_capability);
        UPDATE trellis.messages SET content=v_text WHERE id=(v_result->'message'->>'id')::uuid AND user_id=v_user;
        UPDATE trellis.run_assistant_outputs SET promoted_message_id=(v_result->'message'->>'id')::uuid WHERE id=v_output.id;
        RETURN jsonb_set(v_result,'{message,content}',to_jsonb(v_text));
    END IF;
    RETURN trellis_private.mutate_before_outputs(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_outputs(text,jsonb,text) FROM PUBLIC,anon,authenticated;
COMMIT;
