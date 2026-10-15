-- A model-call allocation can commit even when cancellation prevents its RPC
-- response from reaching the runtime. Reconcile these records with the run.
BEGIN;
-- All runtime mutations lock the run first. Do the same before the older
-- call-update implementation locks its call, so terminal cleanup cannot form
-- a run -> call / call -> run deadlock with an in-flight usage update.
ALTER FUNCTION trellis_private.mutate(text,jsonb,text)
    RENAME TO mutate_before_terminal_model_calls;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_run_id uuid; v_user uuid:=trellis_private.require_user();
BEGIN
    IF p_action='update_model_call' THEN
        SELECT run_id INTO v_run_id FROM trellis.model_calls
        WHERE id=(p_payload->>'call_id')::uuid AND user_id=v_user;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'model call not found' USING ERRCODE='22023';
        END IF;
        PERFORM trellis_private.require_run(v_run_id,p_capability);
    END IF;
    RETURN trellis_private.mutate_before_terminal_model_calls(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_terminal_model_calls(text,jsonb,text)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

CREATE FUNCTION trellis_private.finalize_run_model_calls() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    UPDATE trellis.model_calls SET
        status=CASE WHEN NEW.status='failed' THEN 'failed' ELSE 'cancelled' END,
        finished_at=COALESCE(NEW.finished_at,now()),
        error_code=COALESCE(error_code,'run_terminated'),updated_at=now()
    WHERE user_id=NEW.user_id AND run_id=NEW.id AND status IN ('pending','streaming');
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.finalize_run_model_calls()
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
CREATE TRIGGER finalize_run_model_calls AFTER UPDATE OF status ON trellis.runs
FOR EACH ROW WHEN(NEW.status IN ('completed','failed','cancelled','interrupted'))
EXECUTE FUNCTION trellis_private.finalize_run_model_calls();

-- Existing terminal runs will not transition again. Close their orphaned calls
-- during the upgrade, without changing reported usage or already closed calls.
UPDATE trellis.model_calls c SET
    status=CASE WHEN r.status='failed' THEN 'failed' ELSE 'cancelled' END,
    finished_at=COALESCE(r.finished_at,now()),
    error_code=COALESCE(c.error_code,'run_terminated'),updated_at=now()
FROM trellis.runs r
WHERE c.user_id=r.user_id AND c.run_id=r.id AND c.status IN ('pending','streaming')
    AND r.status IN ('completed','failed','cancelled','interrupted');
COMMIT;
