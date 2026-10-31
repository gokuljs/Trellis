BEGIN;

ALTER TABLE trellis.runs
    ADD COLUMN research_policy_version text NOT NULL DEFAULT 'bounded-research-v1'
        CHECK (research_policy_version IN ('bounded-research-v1','bounded-research-v2')),
    ADD COLUMN execution_budget_seconds integer CHECK (execution_budget_seconds IN (600,1200,2700));
UPDATE trellis.runs SET execution_budget_seconds=CASE WHEN budget_preset='longer' THEN 1200 ELSE 600 END
    WHERE run_mode='research';
ALTER TABLE trellis.runs ADD CONSTRAINT research_execution_profile_present
    CHECK (run_mode<>'research' OR execution_budget_seconds IS NOT NULL);
GRANT SELECT (research_policy_version,execution_budget_seconds) ON trellis.runs TO authenticated;

CREATE FUNCTION trellis_private.current_research_policy() RETURNS text
LANGUAGE sql IMMUTABLE SET search_path='' AS $$ SELECT 'bounded-research-v1'::text $$;
REVOKE ALL ON FUNCTION trellis_private.current_research_policy() FROM PUBLIC,anon,authenticated;

CREATE OR REPLACE FUNCTION trellis_private.attach_research_mode() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    NEW.run_mode:=COALESCE(NULLIF(current_setting('trellis.run_mode',true),''),'chat');
    IF NEW.run_mode='research' THEN
        NEW.research_policy_version:=trellis_private.current_research_policy();
        NEW.execution_budget_seconds:=CASE WHEN NEW.budget_preset='longer' THEN
            CASE WHEN NEW.research_policy_version='bounded-research-v2' THEN 2700 ELSE 1200 END ELSE 600 END;
        IF NEW.research_policy_version='bounded-research-v2' THEN
            NEW.deadline_at:=NEW.deadline_at + make_interval(secs=>NEW.execution_budget_seconds - CASE WHEN NEW.budget_preset='longer' THEN 1200 ELSE 600 END);
        END IF;
        NEW.max_model_calls:=CASE WHEN NEW.budget_preset='longer' THEN 120 ELSE 72 END;
        NEW.max_tool_calls:=CASE WHEN NEW.budget_preset='longer' THEN 384 ELSE 192 END;
        NEW.max_total_tokens:=CASE WHEN NEW.budget_preset='longer' THEN 600000 ELSE 300000 END;
        NEW.max_cost_usd:=CASE WHEN NEW.budget_preset='longer' THEN 15 ELSE 6 END;
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.attach_research_mode() FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis_private.preserve_research_profile() RETURNS trigger
LANGUAGE plpgsql SET search_path='' AS $$
BEGIN
    IF NEW.research_policy_version IS DISTINCT FROM OLD.research_policy_version
        OR NEW.execution_budget_seconds IS DISTINCT FROM OLD.execution_budget_seconds THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER preserve_research_profile BEFORE UPDATE ON trellis.runs
    FOR EACH ROW EXECUTE FUNCTION trellis_private.preserve_research_profile();
REVOKE ALL ON FUNCTION trellis_private.preserve_research_profile() FROM PUBLIC,anon,authenticated;

CREATE OR REPLACE FUNCTION trellis_private.research_snapshot(p_run_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('run_id',s.run_id,'user_id',s.user_id,'session_id',s.session_id,
        'policy_version',r.research_policy_version,'execution_budget_seconds',r.execution_budget_seconds,
        'objective',s.objective,'phase',s.phase,'revision',s.revision,'checkpoint',s.checkpoint,
        'pending_clarification',s.pending_clarification,'stop_reason',COALESCE(s.stop_reason,r.stop_reason),
        'report_model_call_id',s.report_model_call_id,
        'remaining_seconds',COALESCE(s.paused_remaining_seconds,GREATEST(0,extract(epoch FROM r.deadline_at-statement_timestamp()))),
        'tasks',COALESCE((SELECT jsonb_agg(to_jsonb(t)-'user_id' ORDER BY t.created_at,t.id)
            FROM trellis.research_tasks t WHERE t.user_id=s.user_id AND t.run_id=s.run_id),'[]'::jsonb),
        'citations',COALESCE((SELECT jsonb_agg(jsonb_build_object('source_id',c.source_id,
            'start_line',c.start_line,'end_line',c.end_line) ORDER BY c.citation_index)
            FROM trellis.web_citations c WHERE c.user_id=s.user_id AND c.model_call_id=s.report_model_call_id),'[]'::jsonb))
    FROM trellis.research_runs s JOIN trellis.runs r ON r.user_id=s.user_id AND r.id=s.run_id
    WHERE s.user_id=(SELECT auth.uid()) AND s.run_id=p_run_id
$$;

CREATE OR REPLACE FUNCTION trellis_private.research_mutate_before_clarification(p_action text,p_payload jsonb,p_capability text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_user uuid:=trellis_private.require_user(); v_run trellis.runs%ROWTYPE;
    v_state trellis.research_runs%ROWTYPE; v_task trellis.research_tasks%ROWTYPE;
    v_call trellis.model_calls%ROWTYPE; v_res trellis.research_reservations%ROWTYPE;
    v_op trellis.research_operations%ROWTYPE; v_message trellis.run_messages%ROWTYPE;
    v_tool trellis.tool_calls%ROWTYPE; v_result jsonb; v_item jsonb; v_event jsonb;
    v_completion jsonb:=p_payload->'completion'; v_assistant jsonb:=p_payload->'assistant_message';
    v_response jsonb; v_tools jsonb:='[]'; v_events jsonb:='[]'; v_terminal jsonb:=p_payload->'terminal_result';
    v_fingerprint text; v_op_id uuid; v_tokens bigint; v_cost numeric; v_count integer;
    v_workers integer; v_rounds integer; v_step integer; v_ordinal integer; v_index integer;
    v_reserved_tokens bigint; v_reserved_cost numeric; v_finalize boolean; v_now timestamptz:=clock_timestamp();
BEGIN
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR octet_length(p_payload::text)>8388608 THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
    IF v_run.run_mode<>'research' OR v_run.generation IS DISTINCT FROM (p_payload->>'generation')::integer
       OR v_run.status<>'running' OR v_run.cancel_requested_at IS NOT NULL OR v_run.deadline_at<=v_now THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    IF p_action='open' THEN
        IF COALESCE(length(p_payload->>'objective'),0) NOT BETWEEN 1 AND 16000 THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        INSERT INTO trellis.research_runs(run_id,user_id,session_id,objective,checkpoint)
            VALUES(v_run.id,v_user,v_run.chat_id,p_payload->>'objective',
                CASE WHEN v_run.research_policy_version='bounded-research-v2' THEN
                    jsonb_build_object('conversation_context',COALESCE((
                        SELECT jsonb_agg(jsonb_build_object('message_id',m.id,'role',m.role,'content',left(m.content,2000)) ORDER BY m.ordinal)
                        FROM (SELECT id,role,content,ordinal FROM trellis.messages
                            WHERE user_id=v_user AND chat_id=v_run.chat_id
                            AND ordinal<(SELECT ordinal FROM trellis.messages WHERE id=v_run.input_message_id AND user_id=v_user)
                            ORDER BY ordinal DESC LIMIT 8) m),'[]'::jsonb))
                ELSE '{}'::jsonb END) ON CONFLICT DO NOTHING;
        IF EXISTS(SELECT 1 FROM trellis.research_runs WHERE run_id=v_run.id AND objective<>p_payload->>'objective') THEN
            RAISE EXCEPTION 'research_operation_conflict' USING ERRCODE='22023';
        END IF;
        RETURN trellis_private.research_snapshot(v_run.id);
    END IF;
    SELECT * INTO v_state FROM trellis.research_runs WHERE user_id=v_user AND run_id=v_run.id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'research_not_found' USING ERRCODE='22023'; END IF;
    v_op_id:=(p_payload->>'operation_id')::uuid;
    v_fingerprint:=encode(sha256(convert_to(jsonb_build_object('action',p_action,'payload',
        CASE WHEN p_action='checkpoint' THEN p_payload-'generation'-'expected_revision' ELSE p_payload-'generation' END)::text,'UTF8')),'hex');
    IF v_op_id IS NOT NULL THEN
        SELECT * INTO v_op FROM trellis.research_operations WHERE user_id=v_user AND run_id=v_run.id AND operation_id=v_op_id;
        IF FOUND THEN
            IF v_op.action<>p_action OR v_op.fingerprint<>v_fingerprint THEN
                RAISE EXCEPTION 'research_operation_conflict' USING ERRCODE='22023';
            END IF;
            RETURN v_op.receipt;
        END IF;
    END IF;
    v_workers:=CASE WHEN v_run.budget_preset='longer' THEN 8 ELSE 4 END;
    v_rounds:=CASE WHEN v_run.budget_preset='longer' THEN 4 ELSE 2 END;
    IF p_action='create_tasks' THEN
        IF v_op_id IS NULL OR jsonb_typeof(p_payload->'tasks') IS DISTINCT FROM 'array'
           OR jsonb_array_length(p_payload->'tasks') NOT BETWEEN 1 AND 2 THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        v_result:='[]';
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_payload->'tasks') LOOP
            IF COALESCE(length(v_item->>'objective'),0) NOT BETWEEN 1 AND 16000
               OR COALESCE(v_item->>'role','') NOT IN ('lead','worker','writer','reviewer')
               OR COALESCE((v_item->>'round_index')::integer,-1) NOT BETWEEN 0 AND v_rounds
               OR (v_item->>'role'='worker' AND (v_item->>'round_index')::integer=0) THEN
                RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
            END IF;
            SELECT * INTO v_task FROM trellis.research_tasks WHERE user_id=v_user AND run_id=v_run.id AND id=(v_item->>'id')::uuid;
            IF FOUND THEN
                IF v_task.objective<>v_item->>'objective' OR v_task.role<>v_item->>'role'
                   OR v_task.round_index<>(v_item->>'round_index')::integer THEN
                    RAISE EXCEPTION 'research_operation_conflict' USING ERRCODE='22023';
                END IF;
            ELSE
                IF (SELECT count(*) FROM trellis.research_tasks WHERE user_id=v_user AND run_id=v_run.id)>=16
                   OR (v_item->>'role'='worker' AND (SELECT count(*) FROM trellis.research_tasks
                        WHERE user_id=v_user AND run_id=v_run.id AND role='worker')>=v_workers) THEN
                    RAISE EXCEPTION 'research_task_limit' USING ERRCODE='22023';
                END IF;
                INSERT INTO trellis.research_tasks(id,user_id,run_id,objective,role,round_index)
                    VALUES((v_item->>'id')::uuid,v_user,v_run.id,v_item->>'objective',v_item->>'role',(v_item->>'round_index')::integer)
                    RETURNING * INTO v_task;
            END IF;
            v_result:=v_result||jsonb_build_array(to_jsonb(v_task)-'user_id');
        END LOOP;
    ELSIF p_action='checkpoint' THEN
        IF v_op_id IS NULL OR jsonb_typeof(p_payload->'checkpoint') IS DISTINCT FROM 'object'
           OR octet_length((p_payload->'checkpoint')::text)>262144 THEN
            RAISE EXCEPTION 'research_checkpoint_limit' USING ERRCODE='22023';
        END IF;
        SELECT * INTO v_task FROM trellis.research_tasks WHERE user_id=v_user AND run_id=v_run.id AND id=(p_payload->>'task_id')::uuid FOR UPDATE;
        IF NOT FOUND OR v_task.revision IS DISTINCT FROM (p_payload->>'expected_revision')::integer
           OR v_task.status NOT IN ('queued','running') THEN
            RAISE EXCEPTION 'research_revision_changed' USING ERRCODE='22023';
        END IF;
        IF v_run.research_policy_version='bounded-research-v2' THEN
            IF v_task.role='lead' AND p_payload#>'{checkpoint,conversation_context}' IS DISTINCT FROM v_state.checkpoint->'conversation_context' THEN
                RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
            END IF;
            FOR v_item IN SELECT value FROM jsonb_array_elements(
                COALESCE(p_payload#>'{checkpoint,findings}','[]'::jsonb)||
                COALESCE(p_payload#>'{checkpoint,gaps}','[]'::jsonb)||
                COALESCE(p_payload#>'{checkpoint,conflicts}','[]'::jsonb)) LOOP
                FOR v_event IN SELECT value FROM jsonb_array_elements(COALESCE(v_item->'citations','[]'::jsonb)) LOOP
                    IF NOT EXISTS(SELECT 1 FROM trellis.web_sources WHERE user_id=v_user AND session_id=v_run.chat_id AND run_id=v_run.id
                        AND id=(v_event->>'source_id')::uuid AND status IN ('success','partial')
                        AND (v_event->>'start_line')::integer>=1
                        AND (v_event->>'end_line')::integer>=(v_event->>'start_line')::integer
                        AND (v_event->>'end_line')::integer<=line_count) THEN
                        RAISE EXCEPTION 'research_source_changed' USING ERRCODE='22023';
                    END IF;
                END LOOP;
            END LOOP;
        END IF;
        UPDATE trellis.research_tasks SET checkpoint=p_payload->'checkpoint',revision=revision+1,updated_at=v_now,
            status=CASE WHEN COALESCE(p_payload#>>'{checkpoint,stop_reason}','')<>'' THEN 'failed' ELSE status END
            WHERE id=v_task.id AND user_id=v_user RETURNING * INTO v_task;
        IF v_task.role='lead' THEN
            UPDATE trellis.research_runs SET checkpoint=checkpoint||v_task.checkpoint,revision=revision+1,
                stop_reason=COALESCE(v_task.checkpoint->>'stop_reason',stop_reason),
                phase=COALESCE(v_task.checkpoint->>'phase',phase),updated_at=v_now WHERE run_id=v_run.id;
        END IF;
        v_result:=to_jsonb(v_task)-'user_id';
    ELSIF p_action='reserve_call' THEN
        IF v_op_id IS NULL OR v_state.budget_unknown THEN
            RAISE EXCEPTION 'research_usage_unknown' USING ERRCODE='22023';
        END IF;
        v_reserved_tokens:=(p_payload->>'reserved_tokens')::bigint;
        v_reserved_cost:=(p_payload->>'reserved_cost')::numeric;
        v_finalize:=(p_payload->>'finalization')::boolean;
        IF v_reserved_tokens IS NULL OR v_reserved_tokens<1 OR v_reserved_cost IS NULL OR v_reserved_cost<0
           OR v_finalize IS NULL OR jsonb_typeof(p_payload->'request_snapshot') IS DISTINCT FROM 'object'
           OR octet_length((p_payload->'request_snapshot')::text)>4194304 THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        SELECT * INTO v_task FROM trellis.research_tasks WHERE user_id=v_user AND run_id=v_run.id AND id=(p_payload->>'task_id')::uuid FOR UPDATE;
        IF NOT FOUND OR v_task.status NOT IN ('queued','running') OR v_finalize<>(v_task.role IN ('writer','reviewer')) THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        IF EXISTS(SELECT 1 FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id AND task_id=v_task.id AND status IN ('pending','streaming')) THEN
            RAISE EXCEPTION 'research_task_busy' USING ERRCODE='22023';
        END IF;
        IF v_task.role='worker' AND (SELECT count(DISTINCT task_id) FROM trellis.model_calls
            WHERE user_id=v_user AND run_id=v_run.id AND status IN ('pending','streaming'))>=2 THEN
            RAISE EXCEPTION 'research_concurrency_limit' USING ERRCODE='22023';
        END IF;
        SELECT count(*),COALESCE(sum(CASE WHEN status='settled' THEN actual_tokens ELSE reserved_tokens END),0),
            COALESCE(sum(CASE WHEN status='settled' THEN actual_cost ELSE reserved_cost END),0)
            INTO v_count,v_tokens,v_cost FROM trellis.research_reservations WHERE user_id=v_user AND run_id=v_run.id;
        IF v_count>=v_run.max_model_calls OR v_tokens+v_reserved_tokens>v_run.max_total_tokens
           OR v_cost+v_reserved_cost>v_run.max_cost_usd THEN
            RAISE EXCEPTION 'research_budget_exhausted' USING ERRCODE='22023';
        END IF;
        IF NOT v_finalize AND (v_count>=v_run.max_model_calls-4
            OR v_tokens+v_reserved_tokens>v_run.max_total_tokens*0.75 OR v_cost+v_reserved_cost>v_run.max_cost_usd*0.75
            OR extract(epoch FROM v_run.deadline_at-v_now)<v_run.execution_budget_seconds*0.25) THEN
            RAISE EXCEPTION 'research_finalization_reserve' USING ERRCODE='22023';
        END IF;
        IF v_finalize AND (SELECT count(*) FROM trellis.research_reservations WHERE user_id=v_user AND run_id=v_run.id AND finalization)>=4 THEN
            RAISE EXCEPTION 'research_finalization_limit' USING ERRCODE='22023';
        END IF;
        SELECT COALESCE(max(step_index),0)+1 INTO v_step FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id;
        INSERT INTO trellis.model_calls(user_id,run_id,step_index,provider_id,model_id,adapter_kind,status,request_snapshot,started_at,task_id)
            VALUES(v_user,v_run.id,v_step,v_run.provider_id,v_run.model_id,v_run.adapter_kind,'pending',p_payload->'request_snapshot',v_now,v_task.id)
            RETURNING * INTO v_call;
        INSERT INTO trellis.research_reservations(id,user_id,run_id,task_id,model_call_id,reserved_tokens,reserved_cost,finalization,generation)
            VALUES(v_op_id,v_user,v_run.id,v_task.id,v_call.id,v_reserved_tokens,v_reserved_cost,v_finalize,v_run.generation)
            RETURNING * INTO v_res;
        UPDATE trellis.research_tasks SET status='running',updated_at=v_now WHERE id=v_task.id;
        v_result:=jsonb_build_object('reservation',to_jsonb(v_res)-'user_id','model_call',to_jsonb(v_call)-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt');
    ELSIF p_action IN ('commit_generation','reject_generation') THEN
        SELECT * INTO v_call FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id AND id=(p_payload->>'call_id')::uuid FOR UPDATE;
        IF NOT FOUND OR v_call.task_id IS DISTINCT FROM (p_payload->>'task_id')::uuid THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        IF v_call.completion_receipt IS NOT NULL THEN
            IF v_call.completion_fingerprint IS DISTINCT FROM v_fingerprint THEN
                RAISE EXCEPTION 'research_operation_conflict' USING ERRCODE='22023';
            END IF;
            RETURN v_call.completion_receipt;
        END IF;
        SELECT * INTO v_res FROM trellis.research_reservations WHERE user_id=v_user AND run_id=v_run.id AND model_call_id=v_call.id FOR UPDATE;
        IF NOT FOUND OR v_res.status<>'reserved' OR v_call.status<>'streaming'
           OR jsonb_typeof(v_completion) IS DISTINCT FROM 'object'
           OR (p_action='commit_generation' AND COALESCE(jsonb_typeof(v_assistant)='object',false)=COALESCE(jsonb_typeof(v_terminal)='object',false))
           OR (p_action='reject_generation' AND COALESCE(length(p_payload->>'error_code'),0) NOT BETWEEN 1 AND 100) THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        v_tokens:=(v_completion->>'accounted_total_tokens')::bigint;
        v_cost:=(v_completion->>'estimated_cost')::numeric;
        IF v_tokens IS NULL OR v_cost IS NULL THEN
            UPDATE trellis.research_reservations SET status='unknown' WHERE id=v_res.id;
            UPDATE trellis.research_runs SET budget_unknown=true,stop_reason='model_usage_unknown',updated_at=v_now WHERE run_id=v_run.id;
        ELSE
            IF v_tokens<0 OR v_cost<0 OR (p_action='commit_generation' AND (v_tokens>v_res.reserved_tokens OR v_cost>v_res.reserved_cost)) THEN
                RAISE EXCEPTION 'research_reservation_exceeded' USING ERRCODE='22023';
            END IF;
            IF v_tokens>v_res.reserved_tokens OR v_cost>v_res.reserved_cost THEN
                UPDATE trellis.research_runs SET budget_unknown=true,stop_reason='research_reservation_exceeded',updated_at=v_now WHERE run_id=v_run.id;
            END IF;
            UPDATE trellis.research_reservations SET status='settled',actual_tokens=v_tokens,actual_cost=v_cost WHERE id=v_res.id;
        END IF;
        v_response:=v_completion->'response_snapshot';
        IF jsonb_typeof(v_response) IS DISTINCT FROM 'object' OR jsonb_typeof(v_response->'content') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_response->'tool_calls') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_response->'continuation_items') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        PERFORM trellis_private.mutate('update_model_call',v_completion||jsonb_build_object('call_id',v_call.id,'next_status','completed',
            'error_code',CASE WHEN p_action='reject_generation' THEN p_payload->>'error_code' ELSE NULL END),p_capability);
        IF p_action='reject_generation' THEN
            IF v_tokens IS NULL OR v_cost IS NULL THEN RAISE EXCEPTION 'research_usage_unknown' USING ERRCODE='22023'; END IF;
            UPDATE trellis.research_tasks SET status='failed',result=jsonb_build_object('error_code',p_payload->>'error_code'),
                checkpoint=checkpoint||jsonb_build_object('stop_reason',p_payload->>'error_code'),revision=revision+1,updated_at=v_now
                WHERE user_id=v_user AND run_id=v_run.id AND id=v_call.task_id;
        ELSIF jsonb_typeof(v_assistant)='object' THEN
            IF v_assistant->>'role' IS DISTINCT FROM 'assistant' OR v_assistant->>'content' IS DISTINCT FROM v_response->>'content'
               OR v_assistant->'tool_calls' IS DISTINCT FROM v_response->'tool_calls'
               OR v_assistant->'continuation_items' IS DISTINCT FROM v_response->'continuation_items'
               OR jsonb_array_length(v_response->'tool_calls')=0
               OR (SELECT count(*) FROM trellis.tool_calls WHERE user_id=v_user AND run_id=v_run.id)+jsonb_array_length(v_response->'tool_calls')>v_run.max_tool_calls THEN
                RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
            END IF;
            SELECT COALESCE(max(ordinal),0)+1 INTO v_ordinal FROM trellis.run_messages WHERE user_id=v_user AND run_id=v_run.id;
            INSERT INTO trellis.run_messages(user_id,run_id,ordinal,role,content,continuation,model_call_id,task_id)
                VALUES(v_user,v_run.id,v_ordinal,'assistant',v_response->>'content',v_response->'continuation_items',v_call.id,v_call.task_id)
                RETURNING * INTO v_message;
            v_index:=0;
            FOR v_item IN SELECT value FROM jsonb_array_elements(v_response->'tool_calls') LOOP
                IF COALESCE(length(v_item->>'id'),0) NOT BETWEEN 1 AND 200 OR COALESCE(length(v_item->>'name'),0) NOT BETWEEN 1 AND 100
                   OR jsonb_typeof(v_item->'arguments') IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
                END IF;
                IF v_item->>'name' IN ('research_checkpoint','delegate_research','request_research_clarification') THEN
                    SELECT * INTO v_task FROM trellis.research_tasks WHERE user_id=v_user AND run_id=v_run.id AND id=v_call.task_id;
                    IF v_task.role NOT IN ('lead','worker') OR (v_task.role='worker' AND v_item->>'name'<>'research_checkpoint') THEN
                        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
                    END IF;
                END IF;
                INSERT INTO trellis.tool_calls(user_id,run_id,assistant_message_id,call_index,provider_call_id,name,arguments,status,task_id,recovery_policy)
                    VALUES(v_user,v_run.id,v_message.id,v_index,v_item->>'id',v_item->>'name',v_item->'arguments','pending',v_call.task_id,
                        CASE WHEN v_item->>'name' IN ('research_checkpoint','delegate_research','request_research_clarification') THEN 'checkpointed' ELSE NULL END)
                    RETURNING * INTO v_tool;
                v_tools:=v_tools||jsonb_build_array(trellis_private.public_tool_call(to_jsonb(v_tool)));
                v_event:=trellis_private.append_event(v_user,v_run.id,'tool.call',jsonb_build_object('tool_call_id',v_tool.id,
                    'provider_call_id',v_tool.provider_call_id,'name',v_tool.name,'arguments',v_tool.arguments,
                    'assistant_message_id',v_message.id,'task_id',v_call.task_id));
                v_events:=v_events||jsonb_build_array(v_event);
                v_index:=v_index+1;
            END LOOP;
        ELSE
            IF jsonb_array_length(v_response->'tool_calls')<>0 OR octet_length(v_terminal::text)>262144 THEN
                RAISE EXCEPTION 'research_checkpoint_limit' USING ERRCODE='22023';
            END IF;
            UPDATE trellis.research_tasks SET result=v_terminal,status='completed',revision=revision+1,updated_at=v_now
                WHERE user_id=v_user AND run_id=v_run.id AND id=v_call.task_id;
        END IF;
        v_event:=trellis_private.append_event(v_user,v_run.id,'model.completed',jsonb_build_object('model_call_id',v_call.id,
            'task_id',v_call.task_id,'finish_reason',v_completion->>'finish_reason','finish_kind',v_response->>'finish_kind'));
        v_events:=v_events||jsonb_build_array(v_event);
        SELECT * INTO v_call FROM trellis.model_calls WHERE id=v_call.id;
        v_result:=jsonb_build_object('model_call',to_jsonb(v_call)-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt',
            'tool_calls',v_tools,'events',v_events);
        UPDATE trellis.model_calls SET completion_fingerprint=v_fingerprint,completion_owner_digest=trellis_private.capability_digest(p_capability),
            completion_receipt=v_result WHERE id=v_call.id;
    ELSIF p_action='mark_unknown' THEN
        SELECT * INTO v_call FROM trellis.model_calls WHERE user_id=v_user AND run_id=v_run.id AND id=(p_payload->>'call_id')::uuid FOR UPDATE;
        IF NOT FOUND OR v_call.status NOT IN ('pending','streaming') THEN
            RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
        END IF;
        UPDATE trellis.research_reservations SET status='unknown' WHERE user_id=v_user AND run_id=v_run.id AND model_call_id=v_call.id AND status='reserved';
        UPDATE trellis.model_calls SET error_code=COALESCE(p_payload->>'error_code','model_outcome_unknown') WHERE id=v_call.id;
        UPDATE trellis.research_runs SET budget_unknown=true,stop_reason='model_outcome_unknown',updated_at=v_now WHERE run_id=v_run.id;
        v_result:=jsonb_build_object('stopped',true);
    ELSE
        RAISE EXCEPTION 'unknown research action' USING ERRCODE='22023';
    END IF;
    IF v_op_id IS NOT NULL THEN
        INSERT INTO trellis.research_operations(user_id,run_id,operation_id,action,fingerprint,receipt)
            VALUES(v_user,v_run.id,v_op_id,p_action,v_fingerprint,v_result);
    END IF;
    RETURN v_result;
END;
$$;


CREATE OR REPLACE FUNCTION trellis.read_session_run_summaries(p_chat_id uuid,p_offset integer DEFAULT 0,
    p_limit integer DEFAULT 100,p_turn_id text DEFAULT NULL,p_newest_first boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path='' AS $$
DECLARE v_result jsonb;
BEGIN
    IF p_offset IS NULL OR p_offset<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 101
        OR p_newest_first IS NULL
        OR p_turn_id IS NOT NULL AND length(p_turn_id) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'invalid run summary page' USING ERRCODE='22023';
    END IF;
    WITH page AS MATERIALIZED (
        SELECT id,user_id,chat_id,turn_id,status,provider_id,model_id,adapter_kind,
            upstream_model_id,input_message_id,retry_of,client_request_id,
            max_model_calls,max_tool_calls,budget_preset,max_total_tokens,max_cost_usd,
            deadline_at,cancel_requested_at,lease_expires_at,recovery_count,stop_reason,
            last_event_sequence,error_code,error_message,created_at,started_at,finished_at,
            waiting_tool_call_id,generation,retry_allowed,recovery_reason,uncertain_tool_call_id,run_mode,research_policy_version,execution_budget_seconds
        FROM trellis.runs WHERE user_id=(SELECT auth.uid()) AND chat_id=p_chat_id
            AND (p_turn_id IS NULL OR turn_id=p_turn_id)
        ORDER BY CASE WHEN p_newest_first THEN created_at END DESC,
            CASE WHEN p_newest_first THEN id END DESC,
            CASE WHEN NOT p_newest_first THEN created_at END,
            CASE WHEN NOT p_newest_first THEN id END
        OFFSET p_offset LIMIT p_limit
    ), model_totals AS (
        SELECT c.run_id,count(*) AS model_call_count,
            sum(c.accounted_total_tokens) AS total_tokens,
            bool_and(c.accounted_total_tokens IS NOT NULL
                AND c.status NOT IN ('pending','streaming') AND c.error_code IS DISTINCT FROM 'model_outcome_unknown') AS usage_complete,
            sum(c.estimated_cost) AS estimated_cost_usd,
            bool_and(c.estimated_cost IS NOT NULL
                AND c.status NOT IN ('pending','streaming') AND c.error_code IS DISTINCT FROM 'model_outcome_unknown') AS cost_complete
        FROM trellis.model_calls c JOIN page p ON c.run_id=p.id AND c.user_id=p.user_id
        WHERE c.user_id=(SELECT auth.uid()) GROUP BY c.run_id
    ), tool_totals AS (
        SELECT c.run_id,count(*) AS tool_call_count
        FROM trellis.tool_calls c JOIN page p ON c.run_id=p.id AND c.user_id=p.user_id
        WHERE c.user_id=(SELECT auth.uid()) GROUP BY c.run_id
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'run',to_jsonb(p),
        'model_call_count',COALESCE(m.model_call_count,0),
        'tool_call_count',COALESCE(t.tool_call_count,0),
        'total_tokens',CASE WHEN m.model_call_count IS NULL THEN 0 ELSE m.total_tokens END,
        'usage_complete',COALESCE(m.usage_complete,true),
        'estimated_cost_usd',CASE WHEN m.model_call_count IS NULL THEN 0 ELSE m.estimated_cost_usd END,
        'cost_complete',COALESCE(m.cost_complete,true))
        ORDER BY CASE WHEN p_newest_first THEN p.created_at END DESC,
            CASE WHEN p_newest_first THEN p.id END DESC,
            CASE WHEN NOT p_newest_first THEN p.created_at END,
            CASE WHEN NOT p_newest_first THEN p.id END),'[]'::jsonb)
    INTO v_result FROM page p LEFT JOIN model_totals m ON m.run_id=p.id
        LEFT JOIN tool_totals t ON t.run_id=p.id;
    RETURN v_result;
END;
$$;
REVOKE ALL ON FUNCTION trellis.read_session_run_summaries(uuid,integer,integer,text,boolean)
    FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.read_session_run_summaries(uuid,integer,integer,text,boolean)
    TO authenticated;


COMMIT;
