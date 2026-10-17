\set ON_ERROR_STOP on

DO $check$
BEGIN
    IF has_function_privilege('anon',
        'trellis.list_chat_summaries(uuid)', 'EXECUTE') THEN
        RAISE EXCEPTION 'anon can list chat summaries';
    END IF;
    IF NOT has_function_privilege('authenticated',
        'trellis.list_chat_summaries(uuid)', 'EXECUTE') THEN
        RAISE EXCEPTION 'authenticated cannot list chat summaries';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc
               WHERE oid = 'trellis.list_chat_summaries(uuid)'::regprocedure
                 AND (prosecdef OR provolatile <> 's' OR proconfig IS NOT NULL)) THEN
        RAISE EXCEPTION 'chat summary read must remain invoker, stable, and inlineable';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_indexes
                   WHERE schemaname = 'trellis'
                     AND indexname = 'chats_user_updated'
                     AND indexdef LIKE '%(user_id, updated_at DESC, id DESC)%') THEN
        RAISE EXCEPTION 'chat page-order index is missing';
    END IF;
END
$check$;

INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000c1', 'summary-alice@example.test'),
    ('00000000-0000-0000-0000-0000000000c2', 'summary-bob@example.test');

SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub',
    '00000000-0000-0000-0000-0000000000c1', false);
SELECT trellis.ensure_account();

DO $check$
DECLARE
    v_one uuid;
    v_two uuid;
    v_bob uuid;
    v_first uuid;
    v_cap text := 'ChatSummaryClaimCapabilityWithThirtyTwoBytes123';
    v_rows jsonb;
BEGIN
    v_one := (trellis.mutate('create_session',
        '{"workspace_path":"/alice/one"}'::jsonb)->>'id')::uuid;
    v_two := (trellis.mutate('create_session',
        '{"workspace_path":null}'::jsonb)->>'id')::uuid;
    IF trellis.mutate('claim_turn', pg_catalog.jsonb_build_object(
        'session_id', v_one, 'turn_id', 'summary-turn'), v_cap) <> 'true'::jsonb THEN
        RAISE EXCEPTION 'could not claim test turn';
    END IF;
    PERFORM trellis.mutate('add_user_message', pg_catalog.jsonb_build_object(
        'session_id', v_one, 'turn_id', 'summary-turn', 'content', 'Question'), v_cap);
    PERFORM trellis.mutate('add_assistant_message', pg_catalog.jsonb_build_object(
        'session_id', v_one, 'turn_id', 'summary-turn', 'content', 'Answer',
        'provider', 'openai', 'model', 'openai:gpt-5.5'), v_cap);
    PERFORM trellis.mutate('release_turn', pg_catalog.jsonb_build_object(
        'session_id', v_one, 'turn_id', 'summary-turn'), v_cap);
    PERFORM pg_catalog.pg_sleep(0.002);
    PERFORM trellis.mutate('set_session_workspace', pg_catalog.jsonb_build_object(
        'session_id', v_one, 'workspace_path', '/alice/one'));

    PERFORM set_config('request.jwt.claim.sub',
        '00000000-0000-0000-0000-0000000000c2', false);
    PERFORM trellis.ensure_account();
    v_bob := (trellis.mutate('create_session',
        '{"workspace_path":"/bob/private"}'::jsonb)->>'id')::uuid;
    PERFORM set_config('request.jwt.claim.sub',
        '00000000-0000-0000-0000-0000000000c1', false);

    SELECT pg_catalog.jsonb_agg(pg_catalog.to_jsonb(summary))
    INTO v_rows FROM trellis.list_chat_summaries() AS summary;
    IF pg_catalog.jsonb_array_length(v_rows) <> 2 THEN
        RAISE EXCEPTION 'chat summary list leaked or omitted a chat: %', v_rows;
    END IF;
    SELECT id INTO v_first FROM trellis.list_chat_summaries()
        ORDER BY updated_at DESC, id DESC LIMIT 1;
    IF v_first <> v_one THEN
        RAISE EXCEPTION 'chat summaries were not ordered by latest update';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM trellis.list_chat_summaries() AS summary
                   WHERE summary.id = v_one AND summary.user_id = auth.uid()
                     AND summary.workspace_path = '/alice/one'
                     AND summary.message_count = 2
                     AND summary.created_at IS NOT NULL
                     AND summary.updated_at IS NOT NULL) THEN
        RAISE EXCEPTION 'summary did not count both visible messages';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM trellis.list_chat_summaries() AS summary
                   WHERE summary.id = v_two AND summary.message_count = 0) THEN
        RAISE EXCEPTION 'empty chat summary count was not zero';
    END IF;
    IF EXISTS (SELECT 1 FROM trellis.list_chat_summaries(v_bob)) THEN
        RAISE EXCEPTION 'chat-id filter exposed another account chat';
    END IF;
    IF (SELECT count(*) FROM trellis.list_chat_summaries(v_one)) <> 1 THEN
        RAISE EXCEPTION 'chat-id filter did not return exactly one own chat';
    END IF;
    IF (SELECT count(*) FROM trellis.list_chat_summaries(
        '00000000-0000-0000-0000-0000000000cc')) <> 0 THEN
        RAISE EXCEPTION 'unknown chat-id filter returned a row';
    END IF;

    PERFORM set_config('request.jwt.claim.sub',
        '00000000-0000-0000-0000-0000000000c2', false);
    IF (SELECT count(*) FROM trellis.list_chat_summaries()) <> 1 OR
       NOT EXISTS (SELECT 1 FROM trellis.list_chat_summaries()
                   WHERE id = v_bob AND message_count = 0) THEN
        RAISE EXCEPTION 'second account received the wrong chat summaries';
    END IF;
END
$check$;

RESET ROLE;
