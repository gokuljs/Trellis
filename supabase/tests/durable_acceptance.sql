BEGIN;
DO $$
DECLARE
    v_codepoint integer;
    v_content text;
    v_fingerprint text := trellis_private.submission_fingerprint(
        '{"content":"Hello"}', 'whitespace');
BEGIN
    FOREACH v_codepoint IN ARRAY ARRAY[
        9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760,
        8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
        8232, 8233, 8239, 8287, 12288
    ] LOOP
        v_content := chr(v_codepoint) || 'Hello' || chr(v_codepoint);
        IF trellis_private.submission_fingerprint(
            jsonb_build_object('content', v_content), 'whitespace') <> v_fingerprint THEN
            RAISE EXCEPTION 'submission fingerprint differs from Python str.strip() for U+%',
                upper(lpad(to_hex(v_codepoint), 4, '0'));
        END IF;
        BEGIN
            PERFORM trellis_private.submission_fingerprint(
                jsonb_build_object('content', chr(v_codepoint)), 'whitespace');
            RAISE EXCEPTION 'accepted whitespace-only submission for U+%',
                upper(lpad(to_hex(v_codepoint), 4, '0'));
        EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    END LOOP;
    FOREACH v_codepoint IN ARRAY ARRAY[8203, 65279] LOOP
        IF trellis_private.submission_fingerprint(
            jsonb_build_object('content', chr(v_codepoint) || 'Hello' || chr(v_codepoint)),
            'whitespace') = v_fingerprint THEN
            RAISE EXCEPTION 'trimmed a non-whitespace character for U+%',
                upper(lpad(to_hex(v_codepoint), 4, '0'));
        END IF;
    END LOOP;
    IF trellis_private.submission_fingerprint(
        jsonb_build_object('content', 'Hel' || chr(160) || 'lo'), 'whitespace') = v_fingerprint THEN
        RAISE EXCEPTION 'removed interior whitespace';
    END IF;
END;
$$;
INSERT INTO auth.users(id,email) VALUES
    ('00000000-0000-0000-0000-0000000018a1','durable-a@example.test'),
    ('00000000-0000-0000-0000-0000000018b1','durable-b@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000018a1',true);
SELECT trellis.ensure_account();
DO $$
DECLARE v_chat uuid; v_run jsonb; v_retry jsonb; v_payload jsonb;
BEGIN
    v_chat := (trellis.mutate('create_session','{}')->>'id')::uuid;
    v_payload := jsonb_build_object('session_id',v_chat,'client_request_id','durable-1',
        'content',U&'\00a0\2003 Hello \2003\00a0',
        'model_id','openai:gpt-5.5','budget_preset','conservative',
        'submission',jsonb_build_object('content',U&'\00a0\2003 Hello \2003\00a0','turnId','durable-1'));
    IF trellis.lookup_run(v_chat,'durable-1') IS NOT NULL THEN RAISE EXCEPTION 'lookup created work'; END IF;
    v_run := trellis.accept_run(v_payload);
    IF v_run->>'status' <> 'queued' OR v_run->>'deadline_at' IS NOT NULL
       OR v_run->>'lease_expires_at' IS NOT NULL OR v_run->>'last_event_sequence' <> '1'
       OR v_run->>'execution_mode' <> 'durable' THEN RAISE EXCEPTION 'invalid queued run %',v_run; END IF;
    IF v_run ? 'lease_token' OR v_run ? 'request_fingerprint' THEN RAISE EXCEPTION 'private fields exposed'; END IF;
    v_retry := trellis.accept_run(jsonb_set(v_payload,'{submission,content}','"Hello"')
        || '{"model_id":"missing","budget_preset":"invalid"}');
    IF v_retry->>'id' <> v_run->>'id' THEN RAISE EXCEPTION 'duplicate acceptance'; END IF;
    IF trellis.match_run_request(v_payload)->>'id' <> v_run->>'id'
       OR trellis.lookup_run(v_chat,'durable-1')->>'id' <> v_run->>'id' THEN
        RAISE EXCEPTION 'lookup did not restore accepted run'; END IF;
    BEGIN
        PERFORM trellis.accept_run(jsonb_set(v_payload,'{submission,content}','"Changed"'));
        RAISE EXCEPTION 'accepted mismatching original content';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.accept_run(jsonb_set(v_payload,'{submission,modelId}','"openai:gpt-5.5"'));
        RAISE EXCEPTION 'ignored explicit model presence';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.match_run_request(jsonb_set(v_payload,'{submission,budgetPreset}','"conservative"'));
        RAISE EXCEPTION 'ignored explicit budget presence';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    PERFORM set_config('trellis.test_chat',v_chat::text,true);
    PERFORM set_config('trellis.test_run',v_run->>'id',true);
END;
$$;
RESET ROLE;
DO $$
DECLARE v_run uuid := current_setting('trellis.test_run')::uuid;
BEGIN
    IF (SELECT count(*) FROM trellis_private.jobs WHERE run_id=v_run AND status='ready') <> 1
       OR (SELECT count(*) FROM trellis.messages WHERE run_id=v_run) <> 1
       OR (SELECT count(*) FROM trellis.run_events WHERE run_id=v_run AND event_type='run.queued') <> 1 THEN
        RAISE EXCEPTION 'acceptance records are incomplete or duplicated'; END IF;
    IF (SELECT content FROM trellis.messages WHERE run_id=v_run) <> 'Hello' THEN
        RAISE EXCEPTION 'accepted content differs from Python str.strip()'; END IF;
    IF has_table_privilege('authenticated','trellis_private.jobs','SELECT')
       OR has_table_privilege('authenticated','trellis_private.execution_attempts','INSERT')
       OR has_function_privilege('anon','trellis.accept_run(jsonb)','EXECUTE') THEN
        RAISE EXCEPTION 'durable queue grants are too broad'; END IF;
END;
$$;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000018b1',true);
SELECT trellis.ensure_account();
DO $$
BEGIN
    BEGIN
        PERFORM trellis.lookup_run(current_setting('trellis.test_chat')::uuid,'durable-1');
        RAISE EXCEPTION 'foreign lookup succeeded';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.accept_run(jsonb_build_object('session_id',current_setting('trellis.test_chat'),
            'client_request_id','foreign','content','Hello','model_id','openai:gpt-5.5',
            'budget_preset','conservative','submission',jsonb_build_object('content','Hello')));
        RAISE EXCEPTION 'foreign acceptance succeeded';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END;
$$;
RESET ROLE;
CREATE FUNCTION pg_temp.fail_durable_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'injected job storage failure'; END;
$$;
CREATE TRIGGER fail_durable_job BEFORE INSERT ON trellis_private.jobs
    FOR EACH ROW EXECUTE FUNCTION pg_temp.fail_durable_job();
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000018a1',true);
DO $$
DECLARE v_chat uuid;
BEGIN
    v_chat := (trellis.mutate('create_session','{}')->>'id')::uuid;
    BEGIN
        PERFORM trellis.accept_run(jsonb_build_object('session_id',v_chat,'client_request_id','rollback',
            'content','Rollback','model_id','openai:gpt-5.5','budget_preset','conservative',
            'submission',jsonb_build_object('content','Rollback')));
        RAISE EXCEPTION 'injected failure did not fire';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM <> 'injected job storage failure' THEN RAISE; END IF;
    END;
    IF EXISTS(SELECT 1 FROM trellis.messages WHERE chat_id=v_chat)
       OR EXISTS(SELECT 1 FROM trellis.runs WHERE chat_id=v_chat) THEN
        RAISE EXCEPTION 'failed acceptance left partial records'; END IF;
END;
$$;
RESET ROLE;
DROP TRIGGER fail_durable_job ON trellis_private.jobs;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000018a1',true);
DO $$
DECLARE v_chat uuid; v_index integer; v_payload jsonb;
BEGIN
    FOR v_index IN 2..16 LOOP
        v_chat := (trellis.mutate('create_session','{}')->>'id')::uuid;
        v_payload := jsonb_build_object('session_id',v_chat,'client_request_id','capacity-'||v_index,
            'content','Queued','model_id','openai:gpt-5.5','budget_preset','conservative',
            'submission',jsonb_build_object('content','Queued'));
        PERFORM trellis.accept_run(v_payload);
    END LOOP;
    v_chat := (trellis.mutate('create_session','{}')->>'id')::uuid;
    BEGIN
        PERFORM trellis.accept_run(jsonb_set(v_payload,'{session_id}',to_jsonb(v_chat)));
        RAISE EXCEPTION 'account limit exceeded';
    EXCEPTION WHEN invalid_parameter_value THEN
        IF SQLERRM <> 'account run capacity reached' THEN RAISE; END IF;
    END;
    IF trellis.lookup_run(current_setting('trellis.test_chat')::uuid,'durable-1') IS NULL THEN
        RAISE EXCEPTION 'capacity hid accepted request'; END IF;
END;
$$;
ROLLBACK;
