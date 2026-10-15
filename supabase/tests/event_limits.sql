\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id,email) VALUES
('00000000-0000-0000-0000-0000000007a1','event-limit-a@example.test'),
('00000000-0000-0000-0000-0000000007b1','event-limit-b@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000007a1',true);
SELECT trellis.ensure_account();
DO $check$
BEGIN
    IF has_column_privilege('authenticated','trellis.runs','event_storage_count','SELECT')
       OR has_column_privilege('authenticated','trellis.runs','event_storage_bytes','SELECT') THEN
        RAISE EXCEPTION 'private event counters are exposed';
    END IF;
    BEGIN
        UPDATE trellis.runs SET event_storage_count=0;
        RAISE EXCEPTION 'authenticated caller can reset event counters';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    BEGIN
        PERFORM trellis_private.append_event(auth.uid(),gen_random_uuid(),'model.usage','{}'::jsonb);
        RAISE EXCEPTION 'caller can bypass capability checks through append helper';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$check$;
CREATE TEMP TABLE event_limit_fixture(run_id uuid,chat_id uuid,capability text);
DO $check$
DECLARE v_chat uuid; v_result jsonb; v_run uuid;
    v_call uuid;
    v_cap text:='EventLimitCapabilityWithMoreThanThirtyTwoCharacters123';
BEGIN
    v_chat:=(trellis.mutate('create_session','{}'::jsonb)->>'id')::uuid;
    v_result:=trellis.mutate('create_run',jsonb_build_object('session_id',v_chat,'turn_id','count',
        'client_request_id','count','content','Hello','model_id','openai:gpt-5.5'),v_cap);
    v_run:=(v_result->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,
        'next_status','running','event_type','run.started'),v_cap);
    INSERT INTO event_limit_fixture VALUES(v_run,v_chat,v_cap);
    v_call:=(trellis.mutate('create_model_call',jsonb_build_object('run_id',v_run,
        'step_index',1,'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),v_cap)->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',v_call,'next_status','streaming'),v_cap);
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',v_call,'next_status','completed'),v_cap);
    PERFORM trellis.mutate('record_assistant_message',jsonb_build_object('run_id',v_run,'model_call_id',v_call,
        'message',jsonb_build_object('role','assistant','content','','tool_calls',jsonb_build_array(
            jsonb_build_object('id','pending','name','lookup_record','arguments','{}'::jsonb)))),v_cap);
    BEGIN
        PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',v_run,
            'event_type','model.usage','data',jsonb_build_object('secret',repeat('x',262144))),v_cap);
        RAISE EXCEPTION 'oversized single event accepted';
    EXCEPTION WHEN invalid_parameter_value THEN
        IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
    END;
    BEGIN
        PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',v_run,
            'event_type','model.usage','data','{}'::jsonb),'WrongCapabilityWithMoreThanThirtyTwoCharacters123');
        RAISE EXCEPTION 'wrong capability accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END;
$check$;
RESET ROLE;
-- A disposable fixture fills persisted history without thousands of RPC calls.
INSERT INTO trellis.run_events(user_id,run_id,sequence,event_type,data)
SELECT '00000000-0000-0000-0000-0000000007a1',f.run_id,s,'model.usage','{}'::jsonb
FROM event_limit_fixture f CROSS JOIN LATERAL generate_series(
    (SELECT max(sequence)+1 FROM trellis.run_events WHERE run_id=f.run_id),8176) s;
UPDATE trellis.runs r SET last_event_sequence=8176,event_storage_count=8176,
    event_storage_bytes=(SELECT sum(octet_length(data::text)) FROM trellis.run_events e WHERE e.run_id=r.id)
WHERE id=(SELECT run_id FROM event_limit_fixture);
SET LOCAL ROLE authenticated;
DO $check$
DECLARE f record; v_event jsonb;
BEGIN
    SELECT * INTO f FROM event_limit_fixture;
    BEGIN
        PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',f.run_id,
            'event_type','model.usage','data','{}'::jsonb),f.capability);
        RAISE EXCEPTION 'ordinary event consumed terminal count reserve';
    EXCEPTION WHEN invalid_parameter_value THEN
        IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
    END;
    v_event:=trellis.mutate('transition_run_record',jsonb_build_object('run_id',f.run_id,
        'next_status','failed','event_type','run.failed','data',jsonb_build_object('code','event_storage_limit')),
        f.capability);
    IF v_event->>'sequence'<>'8177' THEN RAISE EXCEPTION 'terminal event could not use reserve'; END IF;
    IF (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id)<>8177 THEN
        RAISE EXCEPTION 'old replay events were lost or duplicated';
    END IF;
    PERFORM set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000007b1',true);
    IF EXISTS(SELECT 1 FROM trellis.run_events WHERE run_id=f.run_id) THEN
        RAISE EXCEPTION 'other account can replay bounded events';
    END IF;
    BEGIN
        PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',f.run_id,
            'event_type','model.usage','data','{}'::jsonb),f.capability);
        RAISE EXCEPTION 'other account can append with known capability';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    PERFORM set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000007a1',true);
END;
$check$;
RESET ROLE;
DO $check$
DECLARE r trellis.runs%ROWTYPE; f record;
BEGIN
    SELECT * INTO f FROM event_limit_fixture;
    SELECT * INTO r FROM trellis.runs WHERE id=f.run_id;
    IF EXISTS(SELECT 1 FROM trellis.tool_calls WHERE run_id=r.id AND status IN ('pending','running')) THEN
        RAISE EXCEPTION 'terminal run left pending tool records at event cap';
    END IF;
    IF r.event_storage_count<>8177 OR r.event_storage_bytes<>(
        SELECT sum(octet_length(data::text)) FROM trellis.run_events WHERE run_id=r.id) THEN
        RAISE EXCEPTION 'event counters do not match persisted replay';
    END IF;
    BEGIN
        UPDATE trellis.runs SET event_storage_count=8193 WHERE id=r.id;
        RAISE EXCEPTION 'counter hard limit accepted overflow';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        UPDATE trellis.runs SET event_storage_count=8192,last_event_sequence=8192 WHERE id=r.id;
        PERFORM trellis_private.append_event(r.user_id,r.id,'run.failed','{}'::jsonb);
        RAISE EXCEPTION 'reserved event exceeded count hard limit';
    EXCEPTION WHEN invalid_parameter_value THEN
        IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
    END;
    BEGIN
        UPDATE trellis.runs SET event_storage_bytes=16777216 WHERE id=r.id;
        PERFORM trellis_private.append_event(r.user_id,r.id,'run.failed','{}'::jsonb);
        RAISE EXCEPTION 'reserved event exceeded byte hard limit';
    EXCEPTION WHEN invalid_parameter_value THEN
        IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
    END;
    -- Updating completion identities must account for any new JSON bytes too.
    UPDATE trellis.run_events SET data=data||jsonb_build_object('output_id','identity')
        WHERE run_id=r.id AND sequence=8177;
    IF (SELECT event_storage_bytes FROM trellis.runs WHERE id=r.id)<>(
        SELECT sum(octet_length(data::text)) FROM trellis.run_events WHERE run_id=r.id) THEN
        RAISE EXCEPTION 'event enrichment bypassed byte accounting';
    END IF;
END;
$check$;
SET LOCAL ROLE authenticated;
TRUNCATE event_limit_fixture;
DO $check$
DECLARE v_chat uuid; v_run uuid; v_result jsonb; v_call uuid; v_event jsonb; v_index integer;
    v_remaining integer; v_case integer; v_cap text:='EventBytesCapabilityWithMoreThanThirtyTwoCharacters123';
BEGIN
    FOR v_case IN 0..1 LOOP
    v_chat:=(trellis.mutate('create_session','{}'::jsonb)->>'id')::uuid;
    v_run:=(trellis.mutate('create_run',jsonb_build_object('session_id',v_chat,'turn_id','bytes',
        'client_request_id','bytes','content','Hello','model_id','openai:gpt-5.5'),v_cap)->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,
        'next_status','running','event_type','run.started'),v_cap);
    v_call:=(trellis.mutate('create_model_call',jsonb_build_object('run_id',v_run,
        'step_index',1,'model_id','openai:gpt-5.5','request_snapshot','{}'::jsonb),v_cap)->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',v_call,'next_status','streaming'),v_cap);
    PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',v_run,
        'model_call_id',v_call,'batch_index',0,'text','Done.'),v_cap);
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',v_call,'next_status','completed'),v_cap);
    FOR v_index IN 1..63 LOOP
        PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',v_run,
            'event_type','model.usage','data',jsonb_build_object('value',repeat('x',262131))),v_cap);
    END LOOP;
    SELECT 16711680-sum(octet_length(data::text)) INTO v_remaining FROM trellis.run_events WHERE run_id=v_run;
    PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',v_run,
        'event_type','model.usage','data',jsonb_build_object('text',repeat('x',v_remaining-12))),v_cap);
    BEGIN
        PERFORM trellis.mutate('append_run_event',jsonb_build_object('run_id',v_run,
            'event_type','model.usage','data','{}'::jsonb),v_cap);
        RAISE EXCEPTION 'ordinary event consumed reserved terminal bytes';
    EXCEPTION WHEN invalid_parameter_value THEN
        IF SQLERRM<>'event_storage_limit' THEN RAISE; END IF;
    END;
    IF v_case=0 THEN
    v_event:=trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,
        'next_status','cancelling','event_type','run.cancellation_requested','data','{}'::jsonb),v_cap);
    v_event:=trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,
        'next_status','cancelled','event_type','run.cancelled','data','{}'::jsonb),v_cap);
    ELSE
        v_result:=trellis.mutate('complete_run',jsonb_build_object('run_id',v_run,'content','Done.'),v_cap);
        IF v_result->'events'->0->'data'->>'output_id' IS NULL THEN
            RAISE EXCEPTION 'final answer completion lost its output identity';
        END IF;
    END IF;
    INSERT INTO event_limit_fixture VALUES(v_run,v_chat,v_cap);
    END LOOP;
END;
$check$;
RESET ROLE;
DO $check$
DECLARE v_run uuid; v_chat uuid;
BEGIN
    FOR v_run,v_chat IN SELECT run_id,chat_id FROM event_limit_fixture LOOP
    IF (SELECT event_storage_bytes FROM trellis.runs WHERE id=v_run) >16777216 THEN
        RAISE EXCEPTION 'reserved events exceeded byte hard limit';
    END IF;
    IF (SELECT event_storage_bytes FROM trellis.runs WHERE id=v_run)<>(
        SELECT sum(octet_length(data::text)) FROM trellis.run_events WHERE run_id=v_run) THEN
        RAISE EXCEPTION 'completion byte accounting differs from enriched persisted events';
    END IF;
    DELETE FROM trellis.chats WHERE id=v_chat;
    IF EXISTS(SELECT 1 FROM trellis.run_events WHERE run_id=v_run) THEN
        RAISE EXCEPTION 'chat deletion did not remove retained replay';
    END IF;
    END LOOP;
END;
$check$;
ROLLBACK;
