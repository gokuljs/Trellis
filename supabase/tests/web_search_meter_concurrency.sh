#!/usr/bin/env bash
set -euo pipefail

if [[ ${PGDATABASE:-} != trellis_test && ${PGDATABASE:-} != trellis_stage7_test ]]; then
  printf '%s\n' 'Run this fixture only against a disposable trellis_test or trellis_stage7_test database.' >&2
  exit 1
fi

fixture_user_a=32000000-0000-0000-0000-000000000003
fixture_user_b=32000000-0000-0000-0000-000000000004
fixture_chat_a=32000000-0000-0000-0000-000000000012
fixture_chat_b=32000000-0000-0000-0000-000000000013
key_hash=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
work_dir=$(mktemp -d)
cleanup() {
  psql -X -q -v ON_ERROR_STOP=1 -c "DELETE FROM auth.users WHERE id IN ('$fixture_user_a','$fixture_user_b'); DELETE FROM trellis_private.web_search_attempts WHERE key_hash='$key_hash'; DELETE FROM trellis_private.web_search_months WHERE key_hash='$key_hash'" >/dev/null
  rm -rf "$work_dir"
}
trap cleanup EXIT

psql -X -q -v ON_ERROR_STOP=1 <<SQL
INSERT INTO auth.users(id) VALUES ('$fixture_user_a'),('$fixture_user_b');
INSERT INTO trellis.profiles(id) VALUES ('$fixture_user_a'),('$fixture_user_b');
INSERT INTO trellis.chats(id,user_id) VALUES ('$fixture_chat_a','$fixture_user_a'),('$fixture_chat_b','$fixture_user_b');
SQL
sed -n '/^CREATE FUNCTION pg_temp.web_fixture(/,/^\$\$;/p' supabase/tests/web_search_meter.sql >"$work_dir/setup.sql"
cat >>"$work_dir/setup.sql" <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user_a';
SELECT pg_temp.web_fixture('$fixture_chat_a','concurrent-search-a','web_search')-'model_call_id' AS scope \gset a_
SELECT trellis.web_mutate('open_run',:'a_scope'::jsonb,repeat('a',32)) AS state \gset opened_a_
SET request.jwt.claim.sub='$fixture_user_b';
SELECT pg_temp.web_fixture('$fixture_chat_b','concurrent-search-b','web_search')-'model_call_id' AS scope \gset b_
SELECT trellis.web_mutate('open_run',:'b_scope'::jsonb,repeat('a',32)) AS state \gset opened_b_
SELECT jsonb_build_object('a',:'a_scope'::jsonb,'b',:'b_scope'::jsonb);
SQL
psql -X -qAt -v ON_ERROR_STOP=1 -f "$work_dir/setup.sql" >"$work_dir/scopes.json"
scope_a=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["a"]))' "$work_dir/scopes.json")
scope_b=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["b"]))' "$work_dir/scopes.json")

for consumer in a b; do
  if [[ $consumer == a ]]; then scope_value=$scope_a; else scope_value=$scope_b; fi
  psql -X -qAt -v ON_ERROR_STOP=1 -v scope="$scope_value" -v consumer="$consumer" >"$work_dir/reserve-$consumer.log" <<SQL
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub',(:'scope'::jsonb)->>'user_id',false);
SELECT trellis.web_mutate('reserve',:'scope'::jsonb||jsonb_build_object(
    'reservation_id',CASE WHEN :'consumer'='a' THEN '32000000-0000-0000-0000-000000000060' ELSE '32000000-0000-0000-0000-000000000061' END,
    'operation_key','search:0','usage','{"search_calls":1,"page_attempts":0,"network_requests":1,"response_bytes":1048576,"extracted_bytes":0}'::jsonb),repeat('a',32));
SQL
  (
    if psql -X -qAt -v ON_ERROR_STOP=1 -v scope="$scope_value" -v consumer="$consumer" >"$work_dir/meter-$consumer.log" 2>&1 <<SQL
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub',(:'scope'::jsonb)->>'user_id',false);
SELECT trellis.web_reserve_search_attempt(:'scope'::jsonb,
    (CASE WHEN :'consumer'='a' THEN '32000000-0000-0000-0000-000000000060' ELSE '32000000-0000-0000-0000-000000000061' END)::uuid,
    '$key_hash',1,repeat('a',32));
SQL
    then printf '%s' ok >"$work_dir/meter-$consumer.status"; else printf '%s' rejected >"$work_dir/meter-$consumer.status"; fi
  ) &
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
if [[ $(cat "$work_dir/meter-a.status") == $(cat "$work_dir/meter-b.status") ]]; then
  cat "$work_dir"/meter-*.log >&2
  exit 1
fi
if ! grep -q 'web_search_budget_exhausted' "$work_dir"/meter-*.log; then
  cat "$work_dir"/meter-*.log >&2
  exit 1
fi
if [[ $(cat "$work_dir/meter-a.status") == ok ]]; then winner=a; scope_value=$scope_a; else winner=b; scope_value=$scope_b; fi
psql -X -qAt -v ON_ERROR_STOP=1 -v scope="$scope_value" -v consumer="$winner" >"$work_dir/restarted-client.log" <<SQL
SET ROLE authenticated;
SELECT set_config('request.jwt.claim.sub',(:'scope'::jsonb)->>'user_id',false);
SELECT trellis.web_reserve_search_attempt(:'scope'::jsonb,
    (CASE WHEN :'consumer'='a' THEN '32000000-0000-0000-0000-000000000060' ELSE '32000000-0000-0000-0000-000000000061' END)::uuid,
    '$key_hash',1,repeat('a',32));
SQL
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    ASSERT (SELECT attempts=1 FROM trellis_private.web_search_months WHERE key_hash='$key_hash'
        AND month_start=date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC')::date),'shared-key quota was not durable';
    ASSERT (SELECT count(*)=1 FROM trellis_private.web_search_attempts WHERE key_hash='$key_hash'),'restarted client replay double charged';
END;
\$\$;
SQL
printf '%s\n' 'PASS: shared-key concurrency, durable client restart, quota and idempotent retries'
