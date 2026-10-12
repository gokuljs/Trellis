-- Retire the one-time SQLite import after every started account import seals.
-- Existing account rows remain in place; turn capability and run exclusion
-- checks remain part of the active mutation path.
BEGIN;

LOCK TABLE trellis_private.import_ledger IN ACCESS EXCLUSIVE MODE;

DO $preflight$
BEGIN
    IF EXISTS (
        SELECT 1 FROM trellis_private.import_ledger AS started
        WHERE started.table_name = '__started__' AND started.row_key = 'account'
          AND NOT EXISTS (
              SELECT 1 FROM trellis_private.import_ledger AS sealed
              WHERE sealed.user_id = started.user_id
                AND sealed.table_name = '__sealed__' AND sealed.row_key = 'account'
          )
    ) THEN
        RAISE EXCEPTION
            'unfinished legacy imports must finish before retiring SQLite import support';
    END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION trellis_private.mutate(
    p_action text, p_payload jsonb, p_capability text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_user uuid := trellis_private.require_user();
    v_chat_id uuid;
    v_claim_hash text;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_payload IS NULL OR pg_catalog.jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'mutation payload must be an object' USING ERRCODE = '22023';
    END IF;
    IF p_action IN ('claim_turn','release_turn','add_user_message',
                    'add_assistant_message','create_run') THEN
        v_chat_id := (p_payload->>'session_id')::uuid;
        PERFORM 1 FROM trellis.chats
            WHERE id = v_chat_id AND user_id = v_user FOR UPDATE;
        IF NOT FOUND THEN
            IF p_action = 'claim_turn' THEN RETURN 'false'::jsonb; END IF;
            IF p_action = 'release_turn' THEN RETURN 'null'::jsonb; END IF;
            RAISE EXCEPTION 'session not found' USING ERRCODE = '22023';
        END IF;
        IF p_action = 'create_run' THEN
            IF EXISTS (SELECT 1 FROM trellis.turn_claims
                WHERE user_id = v_user AND chat_id = v_chat_id
                  AND claimed_at > pg_catalog.clock_timestamp() - interval '5 minutes') THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'session_turn_busy';
            END IF;
        ELSIF p_action <> 'release_turn' THEN
            IF EXISTS (SELECT 1 FROM trellis.runs
                WHERE user_id = v_user AND chat_id = v_chat_id
                  AND status IN ('queued','running','waiting_for_approval','cancelling')) THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'session_run_busy';
            END IF;
        END IF;
    END IF;
    IF p_action = 'claim_turn' THEN
        v_claim_hash := trellis_private.capability_digest(p_capability);
        DELETE FROM trellis.turn_claims WHERE user_id = v_user
            AND claimed_at <= v_now - interval '5 minutes';
        INSERT INTO trellis.turn_claims (
            user_id, chat_id, turn_id, claimed_at, claim_token_hash
        ) VALUES (v_user, v_chat_id, p_payload->>'turn_id', v_now,
                  v_claim_hash)
        ON CONFLICT DO NOTHING;
        RETURN pg_catalog.to_jsonb(FOUND);
    ELSIF p_action = 'release_turn' THEN
        v_claim_hash := trellis_private.capability_digest(p_capability);
        DELETE FROM trellis.turn_claims WHERE user_id = v_user
            AND chat_id = v_chat_id AND turn_id = p_payload->>'turn_id'
            AND claim_token_hash = v_claim_hash;
        RETURN 'null'::jsonb;
    ELSIF p_action IN ('add_user_message','add_assistant_message') THEN
        v_claim_hash := trellis_private.capability_digest(p_capability);
        PERFORM 1 FROM trellis.turn_claims
            WHERE user_id = v_user AND chat_id = v_chat_id
              AND turn_id = p_payload->>'turn_id'
              AND claim_token_hash = v_claim_hash
              AND claimed_at > v_now - interval '5 minutes'
            FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'turn capability invalid or expired'
                USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN trellis_private.mutate_v1(p_action, p_payload, p_capability);
END
$$;

CREATE OR REPLACE FUNCTION trellis.ensure_account() RETURNS jsonb
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT trellis_private.ensure_account()
$$;

GRANT EXECUTE ON FUNCTION trellis_private.ensure_account() TO authenticated;

DROP FUNCTION trellis_private.ensure_account_guarded();
DROP FUNCTION trellis.import_snapshot(jsonb,text);
DROP FUNCTION trellis.seal_import(text);
DROP FUNCTION trellis_private.import_snapshot(jsonb,text);
DROP FUNCTION trellis_private.import_snapshot_v1(jsonb);
DROP FUNCTION trellis_private.seal_import(text);
DROP TABLE trellis_private.import_ledger;

COMMIT;
