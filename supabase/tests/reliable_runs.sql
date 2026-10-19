\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES ('19000000-0000-0000-0000-000000000001'),('19000000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES ('19000000-0000-0000-0000-000000000001'),('19000000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) VALUES
    ('19000000-0000-0000-0000-000000000010','19000000-0000-0000-0000-000000000001'),
    ('19000000-0000-0000-0000-000000000011','19000000-0000-0000-0000-000000000001');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','19000000-0000-0000-0000-000000000001',true);
DO $$
DECLARE
    p jsonb := jsonb_build_object('session_id','19000000-0000-0000-0000-000000000010',
        'turn_id','hello','client_request_id','hello','content','[REDACTED]',
        'request_fingerprint',repeat('a',64),'budget_preset','conservative',
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1));
    r jsonb;
    again jsonb;
BEGIN
    ASSERT trellis.mutate('lookup_run',p,NULL) IS NULL;
    r := trellis.mutate('create_run',p,repeat('a',32));
    ASSERT (r->>'lease_acquired')::boolean;
    ASSERT NOT (r->'run' ? 'request_fingerprint') AND NOT (r->'run' ? 'lease_token');
    again := trellis.mutate('create_run',p || '{"model_id":"removed","budget_preset":"removed"}',repeat('a',32));
    ASSERT again = r,'lost response must reconcile the same live capability';
    ASSERT NOT (trellis.mutate('create_run',p,repeat('b',32))->>'lease_acquired')::boolean,
        'another caller cannot acquire the existing execution';
    ASSERT trellis.mutate('lookup_run',p,NULL)->>'id' = r->'run'->>'id';
    BEGIN
        PERFORM trellis.mutate('create_run',p || jsonb_build_object('request_fingerprint',repeat('b',64)),repeat('a',32));
        RAISE EXCEPTION 'changed original secret input was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    ASSERT (SELECT count(*) FROM trellis.runs WHERE client_request_id='hello')=1;
    ASSERT (SELECT count(*) FROM trellis.messages WHERE chat_id=(p->>'session_id')::uuid)=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=(r->'run'->>'id')::uuid)=1;
END $$;
RESET ROLE;
UPDATE trellis.runs SET lease_expires_at=now()-interval '1 second',request_fingerprint=NULL
WHERE user_id='19000000-0000-0000-0000-000000000001';
SET LOCAL ROLE authenticated;
DO $$
DECLARE
    p jsonb := '{"session_id":"19000000-0000-0000-0000-000000000010","turn_id":"hello","client_request_id":"hello","content":"[REDACTED]"}';
BEGIN
    ASSERT NOT (trellis.mutate('match_run_request',p,repeat('a',32))->>'lease_acquired')::boolean,
        'expired ownership must not be regained';
    BEGIN
        PERFORM trellis.mutate('match_run_request',p || '{"submitted_model_id":"different"}',NULL);
        RAISE EXCEPTION 'legacy explicit model change was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
END $$;
SELECT set_config('request.jwt.claim.sub','19000000-0000-0000-0000-000000000002',true);
DO $$
DECLARE p jsonb := '{"session_id":"19000000-0000-0000-0000-000000000010","client_request_id":"hello"}';
BEGIN
    ASSERT trellis.mutate('lookup_run',p,NULL) IS NULL,'lookup must be owner scoped';
    ASSERT trellis.mutate('match_run_request',p,NULL) IS NULL,'matching must be owner scoped';
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_reliable_runs(text,jsonb,text)','EXECUTE');
END $$;
RESET ROLE;
CREATE FUNCTION pg_temp.reject_queued_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'injected acceptance failure'; END $$;
CREATE TRIGGER reject_test_event BEFORE INSERT ON trellis.run_events FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_queued_event();
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','19000000-0000-0000-0000-000000000001',true);
DO $$
DECLARE p jsonb := jsonb_build_object('session_id','19000000-0000-0000-0000-000000000011',
    'turn_id','rollback','client_request_id','rollback','content','rollback',
    'request_fingerprint',repeat('c',64),'budget_preset','conservative',
    'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1));
BEGIN
    BEGIN
        PERFORM trellis.mutate('create_run',p,repeat('c',32));
        RAISE EXCEPTION 'acceptance should fail';
    EXCEPTION WHEN raise_exception THEN
        ASSERT SQLERRM='injected acceptance failure';
    END;
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.runs WHERE client_request_id='rollback');
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.messages WHERE chat_id=(p->>'session_id')::uuid);
END $$;
ROLLBACK;
