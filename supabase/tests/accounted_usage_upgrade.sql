\set ON_ERROR_STOP on
BEGIN;
DO $$
DECLARE
    v_user uuid := '16000000-0000-0000-0000-000000000001';
    v_chat uuid := '16000000-0000-0000-0000-000000000010';
    v_run uuid := '16000000-0000-0000-0000-000000000100';
    v_message uuid := '16000000-0000-0000-0000-000000000200';
    v_model trellis.models%ROWTYPE;
    i integer;
BEGIN
    INSERT INTO auth.users(id) VALUES(v_user);
    INSERT INTO trellis.profiles(id) VALUES(v_user);
    INSERT INTO trellis.chats(id,user_id) VALUES(v_chat,v_user);
    SELECT * INTO STRICT v_model FROM trellis.models WHERE provider_id='openai'
        AND enabled ORDER BY catalog_order LIMIT 1;
    INSERT INTO trellis.runs(id,user_id,chat_id,turn_id,input_message_id,client_request_id,
        status,provider_id,model_id,adapter_kind,upstream_model_id,budget_preset,
        max_model_calls,max_tool_calls,max_total_tokens,max_cost_usd,deadline_at)
    VALUES(v_run,v_user,v_chat,'upgrade',v_message,'upgrade','completed',v_model.provider_id,
        v_model.id,v_model.adapter_kind,v_model.upstream_model_id,'conservative',8,16,100000,2,now());
    INSERT INTO trellis.messages(id,user_id,chat_id,turn_id,ordinal,role,content)
        VALUES(v_message,v_user,v_chat,'upgrade',1,'user','upgrade test');
    FOR i IN 1..5 LOOP
        SELECT * INTO STRICT v_model FROM trellis.models
            WHERE provider_id=CASE WHEN i=2 THEN 'anthropic' ELSE 'openai' END
                AND enabled ORDER BY catalog_order LIMIT 1;
        INSERT INTO trellis.model_calls(user_id,run_id,step_index,provider_id,model_id,
            adapter_kind,status,request_snapshot,input_tokens,output_tokens,
            cached_read_tokens,cache_creation_tokens,estimated_cost)
        VALUES(v_user,v_run,i,v_model.provider_id,v_model.id,v_model.adapter_kind,'completed',
            CASE WHEN i=4 THEN '{"purpose":"native_compaction"}'::jsonb ELSE '{}' END,
            100,CASE WHEN i=3 THEN NULL ELSE 20 END,
            CASE WHEN i=5 THEN 150 ELSE 30 END,10,0.125);
    END LOOP;
END;
$$;
COMMIT;

\ir ../migrations/202610160001_accounted_model_usage.sql

DO $$
BEGIN
    ASSERT (SELECT accounted_total_tokens=120 FROM trellis.model_calls
        WHERE run_id='16000000-0000-0000-0000-000000000100' AND step_index=1),
        'OpenAI cache counts must not be counted twice';
    ASSERT (SELECT accounted_total_tokens=160 FROM trellis.model_calls
        WHERE run_id='16000000-0000-0000-0000-000000000100' AND step_index=2),
        'Anthropic cache counts must be added to input and output';
    ASSERT (SELECT bool_and(accounted_total_tokens IS NULL) FROM trellis.model_calls
        WHERE run_id='16000000-0000-0000-0000-000000000100' AND step_index IN (3,4,5)),
        'unknown, incomplete native, and inconsistent usage must remain unknown';
    ASSERT (SELECT bool_and(estimated_cost=0.125) FROM trellis.model_calls
        WHERE run_id='16000000-0000-0000-0000-000000000100'),
        'upgrading usage must never reprice historical calls';
END;
$$;

DELETE FROM auth.users WHERE id='16000000-0000-0000-0000-000000000001';
