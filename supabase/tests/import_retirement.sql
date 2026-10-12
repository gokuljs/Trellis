\if :{?setup}
INSERT INTO auth.users(id, email) VALUES
    ('00000000-0000-0000-0000-0000000000c5', 'retirement-data@example.test'),
    ('00000000-0000-0000-0000-0000000000c6', 'retirement-partial@example.test');

SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000c5', false);
SELECT trellis.ensure_account();
SELECT trellis.mutate('update_profile', '{"display_name":"Preserved profile"}'::jsonb);
SELECT trellis.mutate('create_session', '{"workspace_path":null}'::jsonb);
RESET ROLE;

INSERT INTO trellis_private.import_ledger(user_id, table_name, row_key, payload_hash)
VALUES ('00000000-0000-0000-0000-0000000000c6', '__started__', 'account', repeat('a', 64));
\elif :{?verify}
DO $check$
DECLARE
    definitions text;
BEGIN
    IF to_regclass('trellis_private.import_ledger') IS NOT NULL
       OR to_regprocedure('trellis.import_snapshot(jsonb,text)') IS NOT NULL
       OR to_regprocedure('trellis.seal_import(text)') IS NOT NULL
       OR to_regprocedure('trellis_private.import_snapshot(jsonb,text)') IS NOT NULL
       OR to_regprocedure('trellis_private.import_snapshot_v1(jsonb)') IS NOT NULL
       OR to_regprocedure('trellis_private.seal_import(text)') IS NOT NULL
       OR to_regprocedure('trellis_private.ensure_account_guarded()') IS NOT NULL THEN
        RAISE EXCEPTION 'legacy import database objects remain';
    END IF;

    SELECT pg_get_functiondef('trellis_private.mutate(text,jsonb,text)'::regprocedure)
        INTO definitions;
    IF definitions LIKE '%import_ledger%'
       OR definitions LIKE '%account_import_in_progress%' THEN
        RAISE EXCEPTION 'ordinary mutations remain blocked by import state';
    END IF;

    IF NOT has_function_privilege('authenticated',
                                  'trellis_private.ensure_account()', 'EXECUTE')
       OR NOT has_function_privilege('authenticated', 'trellis.ensure_account()', 'EXECUTE') THEN
        RAISE EXCEPTION 'account initialization grants changed incorrectly';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM trellis.profiles
        WHERE id = '00000000-0000-0000-0000-0000000000c5'
          AND display_name = 'Preserved profile')
       OR (SELECT count(*) FROM trellis.chats
           WHERE user_id = '00000000-0000-0000-0000-0000000000c5') <> 1 THEN
        RAISE EXCEPTION 'retirement migration changed existing account data';
    END IF;
END
$check$;

SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000c5', false);
SELECT trellis.mutate('create_session', '{"workspace_path":null}'::jsonb);
RESET ROLE;

DO $check$
BEGIN
    IF (SELECT count(*) FROM trellis.chats
        WHERE user_id = '00000000-0000-0000-0000-0000000000c5') <> 2 THEN
        RAISE EXCEPTION 'normal account writes failed after retirement';
    END IF;
END
$check$;
\endif
