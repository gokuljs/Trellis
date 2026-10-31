BEGIN;

CREATE TABLE trellis.research_handoff_holds (
    user_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation>=0),
    reserved_tokens bigint NOT NULL CHECK (reserved_tokens>0),
    reserved_cost numeric NOT NULL CHECK (reserved_cost>=0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(user_id,run_id,task_id),
    FOREIGN KEY(user_id,run_id,task_id) REFERENCES trellis.research_tasks(user_id,run_id,id) ON DELETE CASCADE
);
ALTER TABLE trellis.research_handoff_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE trellis.research_handoff_holds FORCE ROW LEVEL SECURITY;
REVOKE ALL ON trellis.research_handoff_holds FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis_private.research_source_tool_policy() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_policy text;
BEGIN
    SELECT research_policy_version INTO v_policy FROM trellis.runs WHERE user_id=NEW.user_id AND id=NEW.run_id;
    IF NEW.name IN ('web_source_read','web_source_find','web_source_outline') THEN
        IF v_policy<>'bounded-research-v2' THEN RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023'; END IF;
        NEW.recovery_policy:='checkpointed';
    ELSIF v_policy='bounded-research-v2' AND NEW.name='web_source' THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER research_source_tool_policy BEFORE INSERT ON trellis.tool_calls
    FOR EACH ROW EXECUTE FUNCTION trellis_private.research_source_tool_policy();
REVOKE ALL ON FUNCTION trellis_private.research_source_tool_policy() FROM PUBLIC,anon,authenticated;

CREATE OR REPLACE FUNCTION trellis_private.web_scope(p_payload jsonb,p_capability text,p_open boolean DEFAULT false)
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
    IF NOT FOUND OR v_tool.status<>'running'
       OR NOT (v_tool.name IN ('web_search','web_read','web_crawl')
           OR v_tool.name='web_source' AND v_run.research_policy_version='bounded-research-v1'
           OR v_tool.name IN ('web_source_read','web_source_find','web_source_outline') AND v_run.research_policy_version='bounded-research-v2')
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
REVOKE ALL ON FUNCTION trellis_private.web_scope(jsonb,text,boolean) FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis_private.release_research_handoffs() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF TG_TABLE_NAME='runs' THEN
        IF NEW.status<>'running' OR NEW.cancel_requested_at IS NOT NULL OR NEW.generation<>OLD.generation THEN
            DELETE FROM trellis.research_handoff_holds WHERE user_id=NEW.user_id AND run_id=NEW.id;
        END IF;
    ELSIF TG_TABLE_NAME='research_runs' THEN
        IF NEW.budget_unknown THEN
            DELETE FROM trellis.research_handoff_holds WHERE user_id=NEW.user_id AND run_id=NEW.run_id;
        END IF;
    ELSIF NEW.status NOT IN ('queued','running') THEN
        DELETE FROM trellis.research_handoff_holds
            WHERE user_id=NEW.user_id AND run_id=NEW.run_id AND task_id=NEW.id;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER release_root_research_handoffs AFTER UPDATE ON trellis.runs
    FOR EACH ROW EXECUTE FUNCTION trellis_private.release_research_handoffs();
CREATE TRIGGER release_task_research_handoffs AFTER UPDATE ON trellis.research_tasks
    FOR EACH ROW EXECUTE FUNCTION trellis_private.release_research_handoffs();
CREATE TRIGGER release_unknown_research_handoffs AFTER UPDATE ON trellis.research_runs
    FOR EACH ROW EXECUTE FUNCTION trellis_private.release_research_handoffs();
REVOKE ALL ON FUNCTION trellis_private.release_research_handoffs() FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis_private.research_budget_snapshot(p_run_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    WITH root AS (
        SELECT * FROM trellis.runs WHERE user_id=(SELECT auth.uid()) AND id=p_run_id
    ), spent AS (
        SELECT count(*) AS calls,
            COALESCE(sum(CASE WHEN status='settled' THEN actual_tokens ELSE reserved_tokens END),0) AS tokens,
            COALESCE(sum(CASE WHEN status='settled' THEN actual_cost ELSE reserved_cost END),0) AS cost
        FROM trellis.research_reservations WHERE user_id=(SELECT auth.uid()) AND run_id=p_run_id
    ), holds AS (
        SELECT count(*) AS calls,COALESCE(sum(reserved_tokens),0) AS tokens,COALESCE(sum(reserved_cost),0) AS cost
        FROM trellis.research_handoff_holds WHERE user_id=(SELECT auth.uid()) AND run_id=p_run_id
    )
    SELECT jsonb_build_object(
        'used',jsonb_build_object('model_calls',s.calls,'tokens',s.tokens,'estimated_cost',s.cost),
        'held',jsonb_build_object('model_calls',h.calls,'tokens',h.tokens,'estimated_cost',h.cost),
        'collection_remaining',jsonb_build_object('model_calls',greatest(0,r.max_model_calls-4-s.calls-h.calls),
            'tokens',greatest(0,floor(r.max_total_tokens*0.75)-s.tokens-h.tokens),
            'estimated_cost',greatest(0,r.max_cost_usd*0.75-s.cost-h.cost)),
        'finalization_reserve',jsonb_build_object('model_calls',4,'tokens',r.max_total_tokens*0.25,
            'estimated_cost',r.max_cost_usd*0.25,'seconds',r.execution_budget_seconds*0.25),
        'web',COALESCE((SELECT jsonb_build_object('used',w.usage,'limits',w.limits)
            FROM trellis.web_runs w WHERE w.user_id=r.user_id AND w.run_id=r.id),'{}'::jsonb))
    FROM root r CROSS JOIN spent s CROSS JOIN holds h
$$;
REVOKE ALL ON FUNCTION trellis_private.research_budget_snapshot(uuid) FROM PUBLIC,anon,authenticated;

ALTER FUNCTION trellis_private.research_mutate(text,jsonb,text) RENAME TO research_mutate_before_handoffs;
CREATE FUNCTION trellis_private.research_mutate(p_action text,p_payload jsonb,p_capability text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_run trellis.runs%ROWTYPE; v_task trellis.research_tasks%ROWTYPE; v_user uuid;
    v_tokens bigint; v_cost numeric; v_calls integer; v_hold_tokens bigint; v_hold_cost numeric; v_hold_calls integer;
    v_requested_tokens bigint; v_requested_cost numeric; v_finalize boolean; v_handoff boolean;
    v_result jsonb; v_task_id uuid; v_now timestamptz:=clock_timestamp();
BEGIN
    IF p_action NOT IN ('set_handoff','release_handoff','budget_state','reserve_call') THEN
        RETURN trellis_private.research_mutate_before_handoffs(p_action,p_payload,p_capability);
    END IF;
    v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
    IF v_run.research_policy_version<>'bounded-research-v2' THEN
        RETURN trellis_private.research_mutate_before_handoffs(p_action,p_payload,p_capability);
    END IF;
    v_user:=v_run.user_id;
    IF v_run.run_mode<>'research' OR v_run.generation IS DISTINCT FROM (p_payload->>'generation')::integer
        OR v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL OR v_run.deadline_at<=v_now THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    IF p_action='budget_state' THEN
        RETURN trellis_private.research_budget_snapshot(v_run.id);
    END IF;
    v_task_id:=(p_payload->>'task_id')::uuid;
    SELECT * INTO v_task FROM trellis.research_tasks
        WHERE user_id=v_user AND run_id=v_run.id AND id=v_task_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023'; END IF;
    IF p_action='release_handoff' THEN
        DELETE FROM trellis.research_handoff_holds
            WHERE user_id=v_user AND run_id=v_run.id AND task_id=v_task.id;
        RETURN jsonb_build_object('released',true);
    END IF;
    IF EXISTS(SELECT 1 FROM trellis.research_runs WHERE user_id=v_user AND run_id=v_run.id AND budget_unknown) THEN
        RAISE EXCEPTION 'research_usage_unknown' USING ERRCODE='22023';
    END IF;
    IF p_action='reserve_call' AND EXISTS(SELECT 1 FROM trellis.research_reservations
        WHERE user_id=v_user AND run_id=v_run.id AND id=(p_payload->>'operation_id')::uuid) THEN
        RETURN trellis_private.research_mutate_before_handoffs(p_action,p_payload,p_capability);
    END IF;
    v_requested_tokens:=(p_payload->>'reserved_tokens')::bigint;
    v_requested_cost:=(p_payload->>'reserved_cost')::numeric;
    IF jsonb_typeof(p_payload->'reserved_tokens') IS DISTINCT FROM 'number'
        OR jsonb_typeof(p_payload->'reserved_cost') IS DISTINCT FROM 'number'
        OR v_requested_tokens IS NULL OR v_requested_tokens<1 OR v_requested_cost IS NULL OR v_requested_cost<0
        OR v_task.status NOT IN ('queued','running') THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    v_finalize:=p_action='reserve_call' AND COALESCE((p_payload->>'finalization')::boolean,false);
    v_handoff:=p_action='reserve_call' AND COALESCE((p_payload->>'handoff')::boolean,false);
    IF p_action='set_handoff' AND v_task.role<>'worker' OR v_handoff AND (v_finalize OR v_task.role<>'worker') THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    IF p_action='set_handoff' OR v_handoff THEN
        DELETE FROM trellis.research_handoff_holds
            WHERE user_id=v_user AND run_id=v_run.id AND task_id=v_task.id;
    END IF;
    SELECT count(*),COALESCE(sum(CASE WHEN status='settled' THEN actual_tokens ELSE reserved_tokens END),0),
        COALESCE(sum(CASE WHEN status='settled' THEN actual_cost ELSE reserved_cost END),0)
        INTO v_calls,v_tokens,v_cost FROM trellis.research_reservations WHERE user_id=v_user AND run_id=v_run.id;
    SELECT count(*),COALESCE(sum(reserved_tokens),0),COALESCE(sum(reserved_cost),0)
        INTO v_hold_calls,v_hold_tokens,v_hold_cost FROM trellis.research_handoff_holds WHERE user_id=v_user AND run_id=v_run.id;
    IF v_calls+v_hold_calls+1>v_run.max_model_calls
        OR v_tokens+v_hold_tokens+v_requested_tokens>v_run.max_total_tokens
        OR v_cost+v_hold_cost+v_requested_cost>v_run.max_cost_usd THEN
        RAISE EXCEPTION 'research_budget_exhausted' USING ERRCODE='22023';
    END IF;
    IF NOT v_finalize AND (v_calls+v_hold_calls+1>v_run.max_model_calls-4
        OR v_tokens+v_hold_tokens+v_requested_tokens>v_run.max_total_tokens*0.75
        OR v_cost+v_hold_cost+v_requested_cost>v_run.max_cost_usd*0.75
        OR extract(epoch FROM v_run.deadline_at-v_now)<v_run.execution_budget_seconds*0.25) THEN
        RAISE EXCEPTION 'research_finalization_reserve' USING ERRCODE='22023';
    END IF;
    IF p_action='set_handoff' THEN
        INSERT INTO trellis.research_handoff_holds(user_id,run_id,task_id,generation,reserved_tokens,reserved_cost)
            VALUES(v_user,v_run.id,v_task.id,v_run.generation,v_requested_tokens,v_requested_cost);
        RETURN trellis_private.research_budget_snapshot(v_run.id);
    END IF;
    v_result:=trellis_private.research_mutate_before_handoffs(p_action,p_payload,p_capability);
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.research_mutate_before_handoffs(text,jsonb,text) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION trellis_private.research_mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.research_mutate(text,jsonb,text) TO authenticated;

COMMIT;
