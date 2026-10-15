BEGIN;
ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_output_events;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_run trellis.runs%ROWTYPE;
    v_output trellis.run_assistant_outputs%ROWTYPE;
    v_result jsonb;
    v_events jsonb := '[]'::jsonb;
    v_event jsonb;
    v_data jsonb;
BEGIN
    IF p_action IN ('complete_run','record_assistant_message') THEN
        v_run := trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        SELECT * INTO v_output FROM trellis.run_assistant_outputs
        WHERE user_id=v_run.user_id AND run_id=v_run.id
          AND (p_action='complete_run' OR model_call_id=(p_payload->>'model_call_id')::uuid)
        ORDER BY step_index DESC LIMIT 1;
    END IF;
    v_result := trellis_private.mutate_before_output_events(p_action,p_payload,p_capability);
    IF v_output.id IS NOT NULL THEN
        v_data := jsonb_build_object('output_id',v_output.id,'model_call_id',v_output.model_call_id,
            'status','complete','stop_reason',CASE WHEN p_action='complete_run'
                THEN 'final_response' ELSE 'tool_continuation' END);
        IF p_action='complete_run' THEN
            v_data := v_data || jsonb_build_object('promoted_message_id',v_result->'message'->>'id');
        END IF;
        FOR v_event IN SELECT value FROM jsonb_array_elements(v_result->'events') LOOP
            IF v_event->>'event_type' IN ('assistant.completed','assistant.message') THEN
                UPDATE trellis.run_events SET data=data||v_data
                WHERE user_id=v_run.user_id AND run_id=v_run.id AND sequence=(v_event->>'sequence')::integer;
                v_event := jsonb_set(v_event,'{data}',v_event->'data'||v_data);
            END IF;
            v_events := v_events || jsonb_build_array(v_event);
        END LOOP;
        v_result := jsonb_set(v_result,'{events}',v_events);
    END IF;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_output_events(text,jsonb,text) FROM PUBLIC,anon,authenticated;
COMMIT;
