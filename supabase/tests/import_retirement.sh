#!/usr/bin/env bash

set -Eeuo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
migration="$repository_root/supabase/migrations/202610130001_retire_legacy_import.sql"
fixture="$repository_root/supabase/tests/import_retirement.sql"
error_log="$(mktemp)"
trap 'rm -f -- "$error_log"' EXIT

psql -X -v ON_ERROR_STOP=1 -v setup=1 -f "$fixture"

if psql -X -v ON_ERROR_STOP=1 -f "$migration" >"$error_log" 2>&1; then
  echo "retirement migration accepted an unfinished import" >&2
  exit 1
fi

if ! grep -F "unfinished legacy imports" "$error_log" >/dev/null; then
  cat "$error_log" >&2
  echo "retirement migration failed for an unexpected reason" >&2
  exit 1
fi

psql -X -v ON_ERROR_STOP=1 <<'SQL'
DO $check$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM trellis_private.import_ledger
        WHERE user_id = '00000000-0000-0000-0000-0000000000c6'
          AND table_name = '__started__' AND row_key = 'account'
    ) THEN
        RAISE EXCEPTION 'preflight failure changed the unfinished import';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM trellis.profiles
        WHERE id = '00000000-0000-0000-0000-0000000000c5'
          AND display_name = 'Preserved profile')
       OR (SELECT count(*) FROM trellis.chats
           WHERE user_id = '00000000-0000-0000-0000-0000000000c5') <> 1 THEN
        RAISE EXCEPTION 'preflight failure changed existing account data';
    END IF;
END
$check$;

DELETE FROM trellis_private.import_ledger
WHERE user_id = '00000000-0000-0000-0000-0000000000c6'
  AND table_name = '__started__' AND row_key = 'account';
SQL

psql -X -v ON_ERROR_STOP=1 -f "$migration"
psql -X -v ON_ERROR_STOP=1 -v verify=1 -f "$fixture"
