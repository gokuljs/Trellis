\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id,email) VALUES
 ('00000000-0000-0000-0000-0000000008a1','history-a@example.test'),
 ('00000000-0000-0000-0000-0000000008b1','history-b@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000008a1',true);
SELECT trellis.ensure_account();
SELECT trellis.mutate('create_session','{}'::jsonb)->>'id' AS history_chat \gset
RESET ROLE;
INSERT INTO trellis.messages(user_id,chat_id,turn_id,ordinal,role,content,created_at)
 SELECT '00000000-0000-0000-0000-0000000008a1', :'history_chat'::uuid,'old-'||i,i,'user','Message '||i,'2026-01-01T00:00:00Z'
 FROM generate_series(1,101) i;
SET LOCAL ROLE authenticated;
SELECT set_config('test.history_chat',:'history_chat',true);
DO $$
DECLARE chat uuid:=current_setting('test.history_chat')::uuid;
    result jsonb; cursor jsonb; seen uuid[]:='{}'; identity uuid; row jsonb; n integer:=0;
BEGIN
    LOOP
        result:=trellis.read_chat_history(chat,7,(cursor->>'created_at')::timestamptz,cursor->>'kind',(cursor->>'id')::uuid);
        IF jsonb_array_length(result->'timeline_order')>7 OR jsonb_array_length(result->'messages')>7 THEN
            RAISE EXCEPTION 'history page is unbounded'; END IF;
        FOR row IN SELECT value FROM jsonb_array_elements(result->'timeline_order') LOOP
            identity:=(row->>'id')::uuid;
            IF identity=ANY(seen) THEN RAISE EXCEPTION 'duplicate timeline row'; END IF;
            seen:=array_append(seen,identity); n:=n+1;
        END LOOP;
        cursor:=result->'next_before'; EXIT WHEN cursor='null'::jsonb;
        IF n>101 THEN RAISE EXCEPTION 'history cursor did not advance'; END IF;
    END LOOP;
    IF n<>101 THEN RAISE EXCEPTION 'history lost rows with tied timestamps'; END IF;
    BEGIN
        PERFORM trellis.read_chat_history(chat,101);
        RAISE EXCEPTION 'oversized history page accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    BEGIN
        PERFORM trellis.read_chat_history(chat,7,now(),NULL,gen_random_uuid());
        RAISE EXCEPTION 'incomplete cursor accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    result:=trellis.read_chat_summaries_page(1);
    IF jsonb_array_length(result->'items')<>1 THEN RAISE EXCEPTION 'session page missing'; END IF;
END; $$;
-- Output-only pages must remain bounded even when a turn has many attempts.
DO $$
DECLARE chat uuid:=current_setting('test.history_chat')::uuid;
    r jsonb; c jsonb; run uuid; cap text:='HistoryPagingCapabilityWithEnoughRandomCharacters123';
    result jsonb;
BEGIN
    r:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','current','client_request_id','current',
        'content','Current user request','model_id','openai:gpt-5.5'),cap);
    run:=(r->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,'next_status','running','event_type','run.started'),cap);
    FOR i IN 1..4 LOOP
        c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',i,'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),cap);
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming'),cap);
        PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',run,'model_call_id',c->>'id','batch_index',0,'text','Partial '||i),cap);
        PERFORM trellis.mutate('finish_assistant_output',jsonb_build_object('run_id',run,'model_call_id',c->>'id','stop_reason','tool_continuation'),cap);
    END LOOP;
    result:=trellis.read_chat_history(chat,3);
    IF jsonb_array_length(result->'timeline_order')<>3 OR jsonb_array_length(result->'assistant_outputs')>3 THEN
        RAISE EXCEPTION 'partial output page unbounded'; END IF;
END; $$;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000008b1',true);
SELECT trellis.ensure_account();
DO $$ BEGIN
    IF jsonb_array_length(trellis.read_chat_history(current_setting('test.history_chat')::uuid)->'timeline_order')<>0
       OR jsonb_array_length(trellis.read_chat_summaries_page()->'items')<>0 THEN
        RAISE EXCEPTION 'another account read history'; END IF;
END; $$;
RESET ROLE;
DO $$ BEGIN
    IF has_function_privilege('anon','trellis.read_chat_history(uuid,integer,timestamptz,text,uuid)','EXECUTE')
       OR has_function_privilege('anon','trellis.read_chat_summaries_page(integer,timestamptz,uuid)','EXECUTE') THEN
        RAISE EXCEPTION 'anonymous history RPC access'; END IF;
END; $$;
ROLLBACK;
