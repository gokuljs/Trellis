\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id,email)
    VALUES('30303030-0000-0000-0000-000000000001','durable-legacy@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','30303030-0000-0000-0000-000000000001',true);
SELECT trellis.ensure_account();

DO $$
DECLARE v_chat uuid; v_payload jsonb; v_cap text := repeat('q',40);
BEGIN
    v_chat := (trellis.mutate('create_session','{}')->>'id')::uuid;
    ASSERT trellis.mutate('claim_turn',jsonb_build_object(
        'session_id',v_chat,'turn_id','legacy'),v_cap) = 'true'::jsonb;
    PERFORM trellis.mutate('add_user_message',jsonb_build_object(
        'session_id',v_chat,'turn_id','legacy','content','Original input'),v_cap);
    v_payload := jsonb_build_object('session_id',v_chat,'client_request_id','durable',
        'content','Durable input','model_id','openai:gpt-5.5','budget_preset','conservative',
        'submission',jsonb_build_object('content','Durable input'));
    BEGIN
        PERFORM trellis.accept_run(v_payload);
        RAISE EXCEPTION 'accepted durable work while a legacy turn was running';
    EXCEPTION WHEN SQLSTATE 'PT409' THEN
        ASSERT SQLERRM = 'session_turn_busy';
    END;
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.runs WHERE chat_id=v_chat),
        'rejected admission must not create a run';
    ASSERT (SELECT count(*) FROM trellis.messages WHERE chat_id=v_chat)=1,
        'rejected admission must not create a message';
    PERFORM trellis.mutate('add_assistant_message',jsonb_build_object(
        'session_id',v_chat,'turn_id','legacy','content','Original answer',
        'provider','openai','model','gpt-5.5'),v_cap);
    PERFORM trellis.mutate('release_turn',jsonb_build_object(
        'session_id',v_chat,'turn_id','legacy'),v_cap);
    ASSERT trellis.mutate('claim_turn',jsonb_build_object(
        'session_id',v_chat,'turn_id','expired'),v_cap) = 'true'::jsonb;
    PERFORM set_config('trellis.test_legacy_chat',v_chat::text,true);
    PERFORM set_config('trellis.test_legacy_payload',v_payload::text,true);
END;
$$;

RESET ROLE;
UPDATE trellis.turn_claims SET claimed_at=clock_timestamp()-interval '6 minutes'
    WHERE chat_id=current_setting('trellis.test_legacy_chat')::uuid;
SET LOCAL ROLE authenticated;
DO $$
DECLARE v_run jsonb;
BEGIN
    v_run := trellis.accept_run(current_setting('trellis.test_legacy_payload')::jsonb);
    ASSERT v_run->>'status'='queued','expired classic claims must not block admission';
    PERFORM set_config('trellis.test_legacy_run',v_run->>'id',true);
END;
$$;

RESET ROLE;
UPDATE trellis.turn_claims SET claimed_at=clock_timestamp()
    WHERE chat_id=current_setting('trellis.test_legacy_chat')::uuid;
SET LOCAL ROLE authenticated;
DO $$
DECLARE v_retry jsonb;
BEGIN
    v_retry := trellis.accept_run(current_setting('trellis.test_legacy_payload')::jsonb
        || '{"model_id":"missing","budget_preset":"invalid"}');
    ASSERT v_retry->>'id'=current_setting('trellis.test_legacy_run'),
        'exact accepted retries must precede mutable admission checks';
END;
$$;

RESET ROLE;
DO $$
BEGIN
    ASSERT (SELECT count(*) FROM trellis_private.jobs
        WHERE run_id=current_setting('trellis.test_legacy_run')::uuid)=1;
    ASSERT (SELECT count(*) FROM trellis.run_events
        WHERE run_id=current_setting('trellis.test_legacy_run')::uuid)=1;
END;
$$;
ROLLBACK;
