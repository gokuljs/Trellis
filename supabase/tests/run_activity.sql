\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
    v_user uuid := '16000000-0000-0000-0000-000000000001';
    v_other uuid := '16000000-0000-0000-0000-000000000002';
    v_chat uuid := '16000000-0000-0000-0000-000000000010';
    v_other_chat uuid := '16000000-0000-0000-0000-000000000011';
    v_model trellis.models%ROWTYPE;
    v_run uuid;
    v_message uuid;
    v_call uuid;
    i integer;
BEGIN
    INSERT INTO auth.users(id) VALUES(v_user),(v_other);
    INSERT INTO trellis.profiles(id) VALUES(v_user),(v_other);
    INSERT INTO trellis.chats(id,user_id) VALUES(v_chat,v_user),(v_other_chat,v_other);
    SELECT * INTO STRICT v_model FROM trellis.models
        WHERE provider_id='openai' AND enabled ORDER BY catalog_order LIMIT 1;
    FOR i IN 1..7 LOOP
        v_run:=('16000000-0000-0000-0000-'||lpad((100+i)::text,12,'0'))::uuid;
        v_message:=('16000000-0000-0000-0000-'||lpad((200+i)::text,12,'0'))::uuid;
        INSERT INTO trellis.runs(id,user_id,chat_id,turn_id,input_message_id,client_request_id,
            status,provider_id,model_id,adapter_kind,upstream_model_id,budget_preset,
            max_model_calls,max_tool_calls,max_total_tokens,max_cost_usd,deadline_at,
            lease_token,lease_expires_at,stop_reason,created_at)
        VALUES(v_run,CASE WHEN i=7 THEN v_other ELSE v_user END,
            CASE WHEN i=7 THEN v_other_chat ELSE v_chat END,'turn-'||i,v_message,'request-'||i,
            CASE WHEN i=5 THEN 'running' WHEN i=6 THEN 'interrupted' ELSE 'completed' END,
            v_model.provider_id,v_model.id,v_model.adapter_kind,v_model.upstream_model_id,
            'conservative',8,16,100000,2,now()+interval '10 minutes',
            trellis_private.capability_digest(repeat('a',32)),now()+interval '5 minutes',
            CASE WHEN i=6 THEN 'lease_expired' ELSE NULL END,
            '2026-10-16 00:00:00+00'::timestamptz+i*interval '1 second');
        INSERT INTO trellis.messages(id,user_id,chat_id,turn_id,ordinal,role,content)
        VALUES(v_message,CASE WHEN i=7 THEN v_other ELSE v_user END,
            CASE WHEN i=7 THEN v_other_chat ELSE v_chat END,'turn-'||i,i,'user','test input');
        IF i NOT IN (1,6,7) THEN
            v_call:=('16000000-0000-0000-0000-'||lpad((300+i)::text,12,'0'))::uuid;
            INSERT INTO trellis.model_calls(id,user_id,run_id,step_index,provider_id,model_id,
                adapter_kind,status,request_snapshot,accounted_total_tokens,estimated_cost)
            VALUES(v_call,v_user,v_run,1,v_model.provider_id,v_model.id,v_model.adapter_kind,
                CASE WHEN i=5 THEN 'streaming' ELSE 'completed' END,'{}',
                CASE i WHEN 2 THEN 30 WHEN 4 THEN 100 WHEN 5 THEN 5 END,
                CASE i WHEN 2 THEN 0.1 WHEN 4 THEN 0.4 WHEN 5 THEN 0.01 END);
            IF i IN (2,4) THEN
                INSERT INTO trellis.model_calls(user_id,run_id,step_index,provider_id,model_id,
                    adapter_kind,status,request_snapshot,accounted_total_tokens,estimated_cost)
                VALUES(v_user,v_run,2,v_model.provider_id,v_model.id,v_model.adapter_kind,
                    'failed','{"purpose":"native_compaction"}',
                    CASE WHEN i=2 THEN 40 END,CASE WHEN i=2 THEN 0.2 END);
            END IF;
            IF i=2 THEN
                INSERT INTO trellis.run_messages(id,user_id,run_id,ordinal,role,content,model_call_id)
                VALUES('16000000-0000-0000-0000-000000000400',v_user,v_run,1,
                    'assistant','test tool request',v_call);
                INSERT INTO trellis.tool_calls(user_id,run_id,assistant_message_id,call_index,
                    provider_call_id,name,arguments,status)
                VALUES(v_user,v_run,'16000000-0000-0000-0000-000000000400',0,'tool-1','test','{}','denied'),
                    (v_user,v_run,'16000000-0000-0000-0000-000000000400',1,'tool-2','test','{}','completed');
            END IF;
        END IF;
    END LOOP;
END;
$$;
SET CONSTRAINTS ALL IMMEDIATE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','16000000-0000-0000-0000-000000000001',true);

DO $$
DECLARE
    v_rows jsonb;
    v_row jsonb;
    v_result jsonb;
BEGIN
    v_rows:=trellis.read_session_run_summaries('16000000-0000-0000-0000-000000000010');
    ASSERT jsonb_array_length(v_rows)=6,'owner must see exactly their six runs';
    ASSERT v_rows::text NOT LIKE '%lease_token%','summaries must exclude lease secrets';
    v_row:=v_rows->0;
    ASSERT (v_row->>'model_call_count')::integer=0 AND (v_row->>'tool_call_count')::integer=0;
    ASSERT (v_row->>'total_tokens')::integer=0 AND (v_row->>'estimated_cost_usd')::numeric=0;
    ASSERT (v_row->>'usage_complete')::boolean AND (v_row->>'cost_complete')::boolean;
    v_row:=v_rows->1;
    ASSERT (v_row->>'model_call_count')::integer=2 AND (v_row->>'tool_call_count')::integer=2,
        'count failed compaction and denied tool calls';
    ASSERT (v_row->>'total_tokens')::integer=70 AND (v_row->>'estimated_cost_usd')::numeric=0.3;
    ASSERT (v_row->>'usage_complete')::boolean AND (v_row->>'cost_complete')::boolean;
    v_row:=v_rows->2;
    ASSERT v_row->'total_tokens'='null'::jsonb AND v_row->'estimated_cost_usd'='null'::jsonb;
    ASSERT NOT (v_row->>'usage_complete')::boolean AND NOT (v_row->>'cost_complete')::boolean;
    v_row:=v_rows->3;
    ASSERT (v_row->>'total_tokens')::integer=100 AND (v_row->>'estimated_cost_usd')::numeric=0.4;
    ASSERT NOT (v_row->>'usage_complete')::boolean AND NOT (v_row->>'cost_complete')::boolean;
    v_row:=v_rows->4;
    ASSERT (v_row->>'total_tokens')::integer=5 AND NOT (v_row->>'usage_complete')::boolean;
    ASSERT NOT (v_row->>'cost_complete')::boolean,'streaming totals remain provisional';
    ASSERT v_rows->5->'run'->>'stop_reason'='lease_expired';
    v_rows:=trellis.read_session_run_summaries('16000000-0000-0000-0000-000000000010',1,2,NULL,true);
    ASSERT jsonb_array_length(v_rows)=2 AND v_rows->0->'run'->>'turn_id'='turn-5'
        AND v_rows->1->'run'->>'turn_id'='turn-4','stable newest-first paging';
    v_rows:=trellis.read_session_run_summaries('16000000-0000-0000-0000-000000000010',0,101,'turn-2');
    ASSERT jsonb_array_length(v_rows)=1 AND v_rows->0->'run'->>'turn_id'='turn-2';
    ASSERT trellis.read_session_run_summaries('16000000-0000-0000-0000-000000000011')='[]'::jsonb,
        'another account cannot be read through the summary RPC';
    BEGIN
        PERFORM trellis.read_session_run_summaries('16000000-0000-0000-0000-000000000010',0,102);
        RAISE EXCEPTION 'unbounded summary page accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    v_result:=trellis.mutate('update_model_call',jsonb_build_object(
        'call_id','16000000-0000-0000-0000-000000000305','next_status','streaming',
        'input_tokens',40,'output_tokens',2,'accounted_total_tokens',42,'estimated_cost',0.5),repeat('a',32));
    ASSERT (v_result->>'accounted_total_tokens')::integer=42
        AND (v_result->>'input_tokens')::integer=40
        AND (v_result->>'estimated_cost')::numeric=0.5,'usage and normalized totals update together';
    v_result:=trellis.mutate('update_model_call',jsonb_build_object(
        'call_id','16000000-0000-0000-0000-000000000305','next_status','streaming'),repeat('a',32));
    ASSERT (v_result->>'accounted_total_tokens')::integer=42,'status-only update preserves total';
    BEGIN
        PERFORM trellis.mutate('update_model_call',jsonb_build_object(
            'call_id','16000000-0000-0000-0000-000000000305','next_status','streaming',
            'input_tokens',999,'accounted_total_tokens',-1),repeat('a',32));
        RAISE EXCEPTION 'negative token count accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    ASSERT (SELECT input_tokens=40 FROM trellis.model_calls
        WHERE id='16000000-0000-0000-0000-000000000305'),'invalid update rolled back';
    v_result:=trellis.mutate('update_model_call',jsonb_build_object(
        'call_id','16000000-0000-0000-0000-000000000305','next_status','streaming',
        'input_tokens',50),repeat('a',32));
    ASSERT v_result->'accounted_total_tokens'='null'::jsonb,'legacy usage update invalidates stale total';
    v_result:=trellis.mutate('update_model_call',jsonb_build_object(
        'call_id','16000000-0000-0000-0000-000000000305','next_status','streaming',
        'clear_accounted_total_tokens',true,'clear_estimated_cost',true),repeat('a',32));
    ASSERT v_result->'accounted_total_tokens'='null'::jsonb AND v_result->'estimated_cost'='null'::jsonb;
    BEGIN
        PERFORM trellis.mutate('update_model_call',jsonb_build_object(
            'call_id','16000000-0000-0000-0000-000000000305','next_status','streaming',
            'accounted_total_tokens',88),repeat('b',32));
        RAISE EXCEPTION 'invalid lease accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$$;

SELECT set_config('request.jwt.claim.sub','16000000-0000-0000-0000-000000000002',true);
DO $$
BEGIN
    ASSERT trellis.read_session_run_summaries('16000000-0000-0000-0000-000000000010')='[]'::jsonb;
    BEGIN
        PERFORM trellis.mutate('update_model_call',jsonb_build_object(
            'call_id','16000000-0000-0000-0000-000000000305','next_status','streaming'),repeat('a',32));
        RAISE EXCEPTION 'another account updated the call';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
END;
$$;
RESET ROLE;
DO $$
BEGIN
    ASSERT NOT has_function_privilege('anon',
        'trellis.read_session_run_summaries(uuid,integer,integer,text,boolean)','EXECUTE');
    ASSERT NOT has_function_privilege('authenticated',
        'trellis_private.mutate_before_accounted_model_usage(text,jsonb,text)','EXECUTE');
    ASSERT NOT (SELECT prosecdef FROM pg_proc
        WHERE oid='trellis.read_session_run_summaries(uuid,integer,integer,text,boolean)'::regprocedure),
        'summary RPC must enforce caller RLS';
END;
$$;
ROLLBACK;
