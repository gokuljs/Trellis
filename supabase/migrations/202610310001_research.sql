BEGIN;

ALTER TABLE trellis.runs ADD COLUMN run_mode text NOT NULL DEFAULT 'chat'
    CHECK (run_mode IN ('chat','research'));
GRANT SELECT (run_mode) ON trellis.runs TO authenticated;

CREATE TABLE trellis.research_runs (
    run_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    objective text NOT NULL CHECK (length(objective) BETWEEN 1 AND 16000),
    phase text NOT NULL DEFAULT 'scoping' CHECK (phase IN ('scoping','researching','checking_conflict',
        'waiting_for_input','writing','reviewing','completed','partial','stopped')),
    revision integer NOT NULL DEFAULT 0 CHECK (revision>=0),
    checkpoint jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(checkpoint)='object' AND octet_length(checkpoint::text)<=262144),
    budget_unknown boolean NOT NULL DEFAULT false,
    paused_remaining_seconds numeric CHECK (paused_remaining_seconds>=0),
    pending_clarification jsonb CHECK (jsonb_typeof(pending_clarification)='object' AND octet_length(pending_clarification::text)<=20000),
    stop_reason text CHECK (length(stop_reason) BETWEEN 1 AND 100),
    report_model_call_id uuid,
    report jsonb CHECK (jsonb_typeof(report)='object' AND octet_length(report::text)<=262144),
    review jsonb CHECK (jsonb_typeof(review)='object' AND octet_length(review::text)<=262144),
    publication_receipt jsonb,
    publication_fingerprint text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id,run_id),
    FOREIGN KEY(user_id,run_id) REFERENCES trellis.runs(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,session_id) REFERENCES trellis.chats(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,report_model_call_id) REFERENCES trellis.model_calls(user_id,id)
);

CREATE TABLE trellis.research_tasks (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    run_id uuid NOT NULL,
    objective text NOT NULL CHECK (length(objective) BETWEEN 1 AND 16000),
    role text NOT NULL CHECK (role IN ('lead','worker','writer','reviewer')),
    round_index integer NOT NULL CHECK (round_index BETWEEN 0 AND 4),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','failed','cancelled')),
    revision integer NOT NULL DEFAULT 0 CHECK (revision>=0),
    checkpoint jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(checkpoint)='object' AND octet_length(checkpoint::text)<=262144),
    result jsonb CHECK (jsonb_typeof(result)='object' AND octet_length(result::text)<=262144),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (role<>'worker' OR round_index>=1),
    UNIQUE(user_id,run_id,id),
    FOREIGN KEY(user_id,run_id) REFERENCES trellis.research_runs(user_id,run_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX research_one_lead ON trellis.research_tasks(user_id,run_id) WHERE role='lead';

CREATE TABLE trellis.research_operations (
    user_id uuid NOT NULL,
    run_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    action text NOT NULL,
    fingerprint text NOT NULL,
    receipt jsonb NOT NULL CHECK (octet_length(receipt::text)<=8388608),
    PRIMARY KEY(user_id,run_id,operation_id),
    FOREIGN KEY(user_id,run_id) REFERENCES trellis.research_runs(user_id,run_id) ON DELETE CASCADE
);

CREATE TABLE trellis.research_reservations (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    model_call_id uuid NOT NULL,
    reserved_tokens bigint NOT NULL CHECK (reserved_tokens>=0),
    reserved_cost numeric NOT NULL CHECK (reserved_cost>=0 AND reserved_cost<='5'::numeric),
    actual_tokens bigint CHECK (actual_tokens>=0),
    actual_cost numeric CHECK (actual_cost>=0),
    finalization boolean NOT NULL,
    status text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','settled','unknown')),
    generation integer NOT NULL CHECK (generation>=0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id,run_id,model_call_id),
    FOREIGN KEY(user_id,run_id,task_id) REFERENCES trellis.research_tasks(user_id,run_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,run_id,model_call_id) REFERENCES trellis.model_calls(user_id,run_id,id) ON DELETE CASCADE,
    CHECK ((status='settled' AND actual_tokens IS NOT NULL AND actual_cost IS NOT NULL)
        OR (status IN ('reserved','unknown') AND actual_tokens IS NULL AND actual_cost IS NULL))
);

DO $policies$
DECLARE v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY['research_runs','research_tasks','research_operations','research_reservations'] LOOP
        EXECUTE format('ALTER TABLE trellis.%I ENABLE ROW LEVEL SECURITY',v_table);
        EXECUTE format('ALTER TABLE trellis.%I FORCE ROW LEVEL SECURITY',v_table);
        EXECUTE format('REVOKE ALL ON trellis.%I FROM PUBLIC,anon,authenticated',v_table);
    END LOOP;
END;
$policies$;

ALTER TABLE trellis.model_calls ADD COLUMN task_id uuid,
    ADD CONSTRAINT research_model_task_fk FOREIGN KEY(user_id,run_id,task_id)
        REFERENCES trellis.research_tasks(user_id,run_id,id);
ALTER TABLE trellis.run_messages ADD COLUMN task_id uuid,
    ADD CONSTRAINT research_message_task_fk FOREIGN KEY(user_id,run_id,task_id)
        REFERENCES trellis.research_tasks(user_id,run_id,id);
ALTER TABLE trellis.tool_calls ADD COLUMN task_id uuid,
    ADD CONSTRAINT research_tool_task_fk FOREIGN KEY(user_id,run_id,task_id)
        REFERENCES trellis.research_tasks(user_id,run_id,id);
GRANT SELECT (task_id) ON trellis.model_calls,trellis.run_messages,trellis.tool_calls TO authenticated;
ALTER TABLE trellis.tool_calls DROP CONSTRAINT tool_calls_user_id_run_id_provider_call_id_key;
CREATE UNIQUE INDEX tool_calls_scoped_provider_id ON trellis.tool_calls(user_id,run_id,
    COALESCE(task_id,'00000000-0000-0000-0000-000000000000'::uuid),provider_call_id);

CREATE FUNCTION trellis_private.attach_research_mode() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    NEW.run_mode:=COALESCE(NULLIF(current_setting('trellis.run_mode',true),''),'chat');
    IF NEW.run_mode='research' THEN
        NEW.max_model_calls:=CASE WHEN NEW.budget_preset='longer' THEN 40 ELSE 24 END;
        NEW.max_tool_calls:=CASE WHEN NEW.budget_preset='longer' THEN 128 ELSE 64 END;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER attach_research_mode BEFORE INSERT ON trellis.runs
    FOR EACH ROW EXECUTE FUNCTION trellis_private.attach_research_mode();

CREATE FUNCTION trellis_private.attach_research_task() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_task uuid;
BEGIN
    IF TG_TABLE_NAME='tool_calls' THEN
        SELECT task_id INTO v_task FROM trellis.run_messages WHERE user_id=NEW.user_id AND run_id=NEW.run_id AND id=NEW.assistant_message_id;
    ELSIF NEW.model_call_id IS NOT NULL THEN
        SELECT task_id INTO v_task FROM trellis.model_calls WHERE user_id=NEW.user_id AND run_id=NEW.run_id AND id=NEW.model_call_id;
    ELSE
        SELECT task_id INTO v_task FROM trellis.tool_calls WHERE user_id=NEW.user_id AND run_id=NEW.run_id AND id=NEW.tool_call_id;
    END IF;
    IF NEW.task_id IS NOT NULL AND NEW.task_id IS DISTINCT FROM v_task THEN
        RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023';
    END IF;
    NEW.task_id:=v_task;
    RETURN NEW;
END;
$$;
CREATE TRIGGER research_message_scope BEFORE INSERT ON trellis.run_messages
    FOR EACH ROW EXECUTE FUNCTION trellis_private.attach_research_task();
CREATE TRIGGER research_tool_scope BEFORE INSERT ON trellis.tool_calls
    FOR EACH ROW EXECUTE FUNCTION trellis_private.attach_research_task();

CREATE FUNCTION trellis_private.research_snapshot(p_run_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('run_id',s.run_id,'user_id',s.user_id,'session_id',s.session_id,
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
CREATE FUNCTION trellis.read_research(p_run_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
    SELECT trellis_private.research_snapshot(p_run_id)
$$;
REVOKE ALL ON FUNCTION trellis_private.research_snapshot(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.research_snapshot(uuid) TO authenticated;
REVOKE ALL ON FUNCTION trellis.read_research(uuid) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.read_research(uuid) TO authenticated;

CREATE FUNCTION trellis_private.research_mutate(p_action text,p_payload jsonb,p_capability text)
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
        INSERT INTO trellis.research_runs(run_id,user_id,session_id,objective)
            VALUES(v_run.id,v_user,v_run.chat_id,p_payload->>'objective') ON CONFLICT DO NOTHING;
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
            OR extract(epoch FROM v_run.deadline_at-v_now)<(CASE WHEN v_run.budget_preset='longer' THEN 1200 ELSE 600 END)*0.25) THEN
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
CREATE FUNCTION trellis.research_mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL) RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT trellis_private.research_mutate(p_action,p_payload,p_capability)
$$;
REVOKE ALL ON FUNCTION trellis_private.research_mutate(text,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION trellis_private.research_mutate(text,jsonb,text) TO authenticated;
REVOKE ALL ON FUNCTION trellis.research_mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.research_mutate(text,jsonb,text) TO authenticated;

CREATE FUNCTION trellis.read_research_task(p_run_id uuid,p_task_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('task',to_jsonb(t)-'user_id',
        'messages',COALESCE((SELECT jsonb_agg(to_jsonb(m)||jsonb_build_object('tool_calls',COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id',c.provider_call_id,'name',c.name,'arguments',c.arguments) ORDER BY c.call_index)
            FROM trellis.tool_calls c WHERE c.user_id=t.user_id AND c.run_id=t.run_id AND c.assistant_message_id=m.id),'[]'::jsonb)) ORDER BY m.ordinal)
            FROM trellis.run_messages m WHERE m.user_id=t.user_id AND m.run_id=t.run_id AND m.task_id=t.id),'[]'::jsonb),
        'calls',COALESCE((SELECT jsonb_agg(to_jsonb(c)-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt' ORDER BY c.step_index)
            FROM trellis.model_calls c WHERE c.user_id=t.user_id AND c.run_id=t.run_id AND c.task_id=t.id),'[]'::jsonb))
    FROM trellis.research_tasks t WHERE t.user_id=(SELECT auth.uid()) AND t.run_id=p_run_id AND t.id=p_task_id
$$;
REVOKE ALL ON FUNCTION trellis.read_research_task(uuid,uuid) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis.read_research_task(uuid,uuid) TO authenticated;

ALTER FUNCTION trellis_private.mutate(text,jsonb,text) RENAME TO mutate_before_research;
CREATE FUNCTION trellis_private.mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_result jsonb; v_previous text; v_mode text:=COALESCE(p_payload->>'run_mode','chat');
BEGIN
    IF p_action='create_run' THEN
        IF v_mode NOT IN ('chat','research') THEN RAISE EXCEPTION 'research_scope_invalid' USING ERRCODE='22023'; END IF;
        v_previous:=current_setting('trellis.run_mode',true);
        PERFORM set_config('trellis.run_mode',v_mode,true);
        v_result:=trellis_private.mutate_before_research(p_action,p_payload,p_capability);
        PERFORM set_config('trellis.run_mode',COALESCE(v_previous,''),true);
        IF v_result#>>'{run,run_mode}' IS DISTINCT FROM v_mode THEN
            RAISE EXCEPTION 'run request conflicts with a previous payload' USING ERRCODE='22023';
        END IF;
        RETURN v_result;
    END IF;
    RETURN trellis_private.mutate_before_research(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.mutate_before_research(text,jsonb,text) FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
REVOKE ALL ON FUNCTION trellis_private.mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.mutate(text,jsonb,text) TO authenticated;

REVOKE ALL ON FUNCTION trellis_private.attach_research_mode(),trellis_private.attach_research_task() FROM PUBLIC,anon,authenticated;

CREATE FUNCTION trellis.research_readiness() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('schema_version',1,'ready',false)
$$;
REVOKE ALL ON FUNCTION trellis.research_readiness() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION trellis.research_readiness() TO anon,authenticated;

ALTER TABLE trellis.web_runs DROP CONSTRAINT web_runs_limits_check;
ALTER TABLE trellis.web_runs ADD CONSTRAINT web_runs_limits_check CHECK (limits IN (
    '{"search_calls":5,"page_attempts":20,"network_requests":300,"response_bytes":67108864,"extracted_bytes":8388608}'::jsonb,
    '{"search_calls":8,"page_attempts":20,"network_requests":300,"response_bytes":67108864,"extracted_bytes":8388608}'::jsonb,
    '{"search_calls":16,"page_attempts":40,"network_requests":300,"response_bytes":67108864,"extracted_bytes":8388608}'::jsonb));

CREATE FUNCTION trellis_private.research_web_limits(p_run trellis.runs) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path='' AS $$
    SELECT jsonb_build_object('search_calls',CASE WHEN p_run.run_mode='chat' THEN 5
        WHEN p_run.budget_preset='longer' THEN 16 ELSE 8 END,
        'page_attempts',CASE WHEN p_run.run_mode='research' AND p_run.budget_preset='longer' THEN 40 ELSE 20 END,
        'network_requests',300,'response_bytes',67108864,'extracted_bytes',8388608)
$$;
CREATE FUNCTION trellis_private.attach_research_web_limits() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_run trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO v_run FROM trellis.runs WHERE user_id=NEW.user_id AND id=NEW.run_id;
    NEW.limits:=trellis_private.research_web_limits(v_run);
    RETURN NEW;
END;
$$;
CREATE TRIGGER research_web_limits BEFORE INSERT ON trellis.web_runs
    FOR EACH ROW EXECUTE FUNCTION trellis_private.attach_research_web_limits();

CREATE OR REPLACE FUNCTION trellis_private.web_usage_change(p_current jsonb,p_delta jsonb,p_subtract boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path='' AS $$
DECLARE v_result jsonb; v_key text; v_value bigint;
    v_limits jsonb:=COALESCE(NULLIF(current_setting('trellis.web_limits',true),'')::jsonb,
        '{"search_calls":5,"page_attempts":20,"network_requests":300,"response_bytes":67108864,"extracted_bytes":8388608}'::jsonb);
BEGIN
    IF NOT trellis_private.web_usage_valid(p_current) OR NOT trellis_private.web_usage_valid(p_delta) THEN
        RAISE EXCEPTION 'web_scope_invalid' USING ERRCODE='22023';
    END IF;
    v_result:=p_current;
    FOR v_key IN SELECT jsonb_object_keys(v_limits) LOOP
        v_value:=(p_current->>v_key)::bigint+(CASE WHEN p_subtract THEN -1 ELSE 1 END)*(p_delta->>v_key)::bigint;
        IF v_value<0 OR v_value>(v_limits->>v_key)::bigint THEN
            RAISE EXCEPTION 'web_budget_exhausted' USING ERRCODE='22023';
        END IF;
        v_result:=jsonb_set(v_result,ARRAY[v_key],to_jsonb(v_value));
    END LOOP;
    RETURN v_result;
END;
$$;
ALTER FUNCTION trellis_private.web_mutate(text,jsonb,text) RENAME TO web_mutate_before_research;
CREATE FUNCTION trellis_private.web_mutate(p_action text,p_payload jsonb,p_capability text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_run trellis.runs%ROWTYPE; v_previous text; v_result jsonb;
BEGIN
    IF p_action IN ('open_run','reserve','settle','record_source','create_crawl','enqueue_pages','claim_page','complete_page','checkpoint_crawl','bind_citations') THEN
        v_run:=trellis_private.require_run((p_payload->>'run_id')::uuid,p_capability);
        v_previous:=current_setting('trellis.web_limits',true);
        PERFORM set_config('trellis.web_limits',trellis_private.research_web_limits(v_run)::text,true);
        v_result:=trellis_private.web_mutate_before_research(p_action,p_payload,p_capability);
        PERFORM set_config('trellis.web_limits',COALESCE(v_previous,''),true);
        RETURN v_result;
    END IF;
    RETURN trellis_private.web_mutate_before_research(p_action,p_payload,p_capability);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.web_mutate_before_research(text,jsonb,text),
    trellis_private.research_web_limits(trellis.runs),trellis_private.attach_research_web_limits() FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION trellis_private.web_mutate(text,jsonb,text) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION trellis_private.web_mutate(text,jsonb,text) TO authenticated;

CREATE OR REPLACE FUNCTION trellis_private.read_recovery_snapshot(p_run_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path='' AS $$
DECLARE v_result jsonb; v_models integer; v_tools integer;
BEGIN
    SELECT max_model_calls,max_tool_calls INTO v_models,v_tools FROM trellis.runs WHERE user_id=(SELECT auth.uid()) AND id=p_run_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    WITH execution AS MATERIALIZED (
        SELECT * FROM trellis.runs WHERE user_id=(SELECT auth.uid()) AND id=p_run_id
    ), checkpoints AS MATERIALIZED (
        SELECT c.* FROM trellis.context_checkpoints c JOIN execution r ON c.chat_id=r.chat_id
        WHERE c.user_id=(SELECT auth.uid()) AND (c.run_id=r.id OR c.revision=(
            SELECT max(revision) FROM trellis.context_checkpoints WHERE user_id=c.user_id AND chat_id=c.chat_id))
        ORDER BY c.revision LIMIT 17
    ), calls AS MATERIALIZED (
        SELECT c.* FROM trellis.model_calls c JOIN execution r ON c.run_id=r.id
        WHERE c.user_id=(SELECT auth.uid()) ORDER BY step_index LIMIT v_models+1
    ), tools AS MATERIALIZED (
        SELECT t.* FROM trellis.tool_calls t JOIN execution r ON t.run_id=r.id
        WHERE t.user_id=(SELECT auth.uid()) ORDER BY t.created_at,t.id LIMIT v_tools+1
    ), exchanges AS MATERIALIZED (
        SELECT m.* FROM trellis.run_messages m JOIN execution r ON m.run_id=r.id
        WHERE m.user_id=(SELECT auth.uid()) ORDER BY ordinal LIMIT v_models+v_tools+1
    ), outputs AS MATERIALIZED (
        SELECT o.* FROM trellis.run_assistant_outputs o JOIN execution r ON o.run_id=r.id
        WHERE o.user_id=(SELECT auth.uid()) ORDER BY step_index LIMIT v_models+1
    ), transcript_head AS MATERIALIZED (
        SELECT m.* FROM trellis.messages m JOIN execution r ON m.chat_id=r.chat_id
        WHERE m.user_id=(SELECT auth.uid()) AND m.ordinal>COALESCE((
            SELECT covered_message_ordinal FROM checkpoints ORDER BY revision DESC LIMIT 1),0)
        ORDER BY ordinal LIMIT 128
    ), transcript AS MATERIALIZED (
        SELECT * FROM transcript_head UNION ALL
        SELECT m.* FROM trellis.messages m JOIN execution r ON m.id=r.input_message_id
            WHERE m.user_id=(SELECT auth.uid()) AND NOT EXISTS(SELECT 1 FROM transcript_head h WHERE h.id=m.id)
    ) SELECT jsonb_build_object(
        'run',(SELECT trellis_private.public_run(to_jsonb(r)) FROM execution r),
        'model_calls',COALESCE((SELECT jsonb_agg(to_jsonb(c)-'completion_fingerprint'-'completion_owner_digest'-'completion_receipt' ORDER BY step_index) FROM calls c),'[]'::jsonb),
        'tool_calls',COALESCE((SELECT jsonb_agg(trellis_private.public_tool_call(to_jsonb(t)) ORDER BY created_at,id) FROM tools t),'[]'::jsonb),
        'run_messages',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY ordinal) FROM exchanges m),'[]'::jsonb),
        'outputs',COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY step_index) FROM outputs o),'[]'::jsonb),
        'messages',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY ordinal) FROM transcript m),'[]'::jsonb),
        'context_checkpoints',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY revision) FROM checkpoints c),'[]'::jsonb),
        'context_revision',(SELECT c.context_checkpoint_revision FROM trellis.chats c JOIN execution r ON c.id=r.chat_id))
    INTO v_result;
    IF octet_length(v_result::text)>16777216 OR jsonb_array_length(v_result->'model_calls')>v_models
       OR jsonb_array_length(v_result->'tool_calls')>v_tools OR jsonb_array_length(v_result->'run_messages')>v_models+v_tools
       OR jsonb_array_length(v_result->'outputs')>v_models OR jsonb_array_length(v_result->'messages')>129
       OR jsonb_array_length(v_result->'context_checkpoints')>16 THEN
        RAISE EXCEPTION 'recovery_snapshot_limit' USING ERRCODE='22023';
    END IF;
    RETURN v_result;
END;
$$;

CREATE FUNCTION trellis_private.record_research_phase() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v_phase text;
BEGIN
    IF NEW.event_type='run.phase' THEN
        v_phase:=CASE NEW.data->>'phase'
            WHEN 'planning' THEN 'scoping'
            WHEN 'scoping' THEN 'scoping'
            WHEN 'web_search' THEN 'researching'
            WHEN 'web_read' THEN 'researching'
            WHEN 'researching' THEN 'researching'
            WHEN 'checking_conflicts' THEN 'checking_conflict'
            WHEN 'checking_conflict' THEN 'checking_conflict'
            WHEN 'drafting' THEN 'writing'
            WHEN 'repairing' THEN 'writing'
            WHEN 'writing' THEN 'writing'
            WHEN 'reviewing' THEN 'reviewing'
            WHEN 'checking_citations' THEN 'reviewing'
            WHEN 'waiting_for_input' THEN 'waiting_for_input'
            ELSE NULL END;
    ELSIF NEW.event_type IN ('run.failed','run.cancelled','run.interrupted') THEN
        v_phase:='stopped';
    END IF;
    IF v_phase IS NOT NULL THEN
        UPDATE trellis.research_runs SET phase=v_phase,revision=revision+1,updated_at=clock_timestamp()
            WHERE user_id=NEW.user_id AND run_id=NEW.run_id AND phase NOT IN ('completed','partial','stopped')
                AND phase IS DISTINCT FROM v_phase;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER research_phase AFTER INSERT ON trellis.run_events
    FOR EACH ROW EXECUTE FUNCTION trellis_private.record_research_phase();
REVOKE ALL ON FUNCTION trellis_private.record_research_phase() FROM PUBLIC,anon,authenticated;

COMMIT;
