#!/usr/bin/env bash
set -euo pipefail

if [[ ${PGDATABASE:-} != trellis_test && ${PGDATABASE:-} != trellis_stage7_test ]]; then
  printf '%s\n' 'Run this fixture only against a disposable trellis_test or trellis_stage7_test database.' >&2
  exit 1
fi

fixture_user=30000000-0000-0000-0000-000000000003
fixture_chat_a=30000000-0000-0000-0000-000000000012
fixture_chat_b=30000000-0000-0000-0000-000000000013
work_dir=$(mktemp -d)
cleanup() {
  psql -X -q -v ON_ERROR_STOP=1 -c "DELETE FROM auth.users WHERE id='$fixture_user'" >/dev/null
  rm -rf "$work_dir"
}
trap cleanup EXIT

psql -X -q -v ON_ERROR_STOP=1 <<SQL
INSERT INTO auth.users(id) VALUES ('$fixture_user');
INSERT INTO trellis.profiles(id) VALUES ('$fixture_user');
INSERT INTO trellis.chats(id,user_id) VALUES ('$fixture_chat_a','$fixture_user'),('$fixture_chat_b','$fixture_user');
SQL
sed -n '/^CREATE FUNCTION pg_temp.web_fixture(/,/^\$\$;/p' supabase/tests/web_evidence.sql >"$work_dir/setup.sql"
cat >>"$work_dir/setup.sql" <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user';
SELECT pg_temp.web_fixture('$fixture_chat_a','concurrent-a') AS scope \gset a_
SELECT pg_temp.web_fixture('$fixture_chat_b','concurrent-b') AS scope \gset b_
SELECT trellis.web_mutate('open_run',:'a_scope'::jsonb,repeat('a',32)) AS state \gset opened_a_
SELECT trellis.web_mutate('open_run',:'b_scope'::jsonb,repeat('a',32)) AS state \gset opened_b_
SELECT jsonb_build_object('a',:'a_scope'::jsonb,'b',:'b_scope'::jsonb);
SQL
psql -X -qAt -v ON_ERROR_STOP=1 -f "$work_dir/setup.sql" >"$work_dir/scopes.json"
scope_a=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["a"]))' "$work_dir/scopes.json")
scope_b=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["b"]))' "$work_dir/scopes.json")

for consumer in a b; do
  (
    if psql -X -qAt -v ON_ERROR_STOP=1 -v scope="$scope_a" -v consumer="$consumer" >"$work_dir/budget-$consumer.log" 2>&1 <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user';
SELECT trellis.web_mutate('reserve',:'scope'::jsonb||jsonb_build_object(
    'reservation_id',CASE WHEN :'consumer'='a' THEN '30000000-0000-0000-0000-000000000050' ELSE '30000000-0000-0000-0000-000000000051' END,
    'operation_key','budget:'||:'consumer','usage',jsonb_build_object('search_calls',0,'page_attempts',0,
        'network_requests',1,'response_bytes',41943040,'extracted_bytes',0)),repeat('a',32));
SQL
    then printf '%s' ok >"$work_dir/budget-$consumer.status"; else printf '%s' rejected >"$work_dir/budget-$consumer.status"; fi
  ) &
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
if [[ $(cat "$work_dir/budget-a.status") == $(cat "$work_dir/budget-b.status") ]]; then
  cat "$work_dir"/budget-*.log >&2
  exit 1
fi
if ! grep -q 'web_budget_exhausted' "$work_dir"/budget-*.log; then
  cat "$work_dir"/budget-*.log >&2
  exit 1
fi

for consumer in a b; do
  if [[ $consumer == a ]]; then scope_value=$scope_a; else scope_value=$scope_b; fi
  psql -X -qAt -v ON_ERROR_STOP=1 -v scope="$scope_value" -v consumer="$consumer" >"$work_dir/reserve-$consumer.log" <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user';
SELECT trellis.web_mutate('reserve',:'scope'::jsonb||jsonb_build_object(
    'reservation_id',CASE WHEN :'consumer'='a' THEN '30000000-0000-0000-0000-000000000060' ELSE '30000000-0000-0000-0000-000000000061' END,
    'operation_key','quota:'||:'consumer','usage',jsonb_build_object('search_calls',0,'page_attempts',1,
        'network_requests',0,'response_bytes',0,'extracted_bytes',153675)),repeat('a',32));
SQL
done

psql -X -q -v ON_ERROR_STOP=1 <<SQL
UPDATE trellis.web_accounts SET retained_bytes=104857600-200000 WHERE user_id='$fixture_user';
SQL
for consumer in a b; do
  if [[ $consumer == a ]]; then scope_value=$scope_a; else scope_value=$scope_b; fi
  (
    if psql -X -qAt -v ON_ERROR_STOP=1 -v scope="$scope_value" -v consumer="$consumer" >"$work_dir/quota-$consumer.log" 2>&1 <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user';
SELECT trellis.web_mutate('record_source',:'scope'::jsonb||jsonb_build_object(
    'reservation_id',CASE WHEN :'consumer'='a' THEN '30000000-0000-0000-0000-000000000060' ELSE '30000000-0000-0000-0000-000000000061' END,
    'source',jsonb_build_object(
        'id',CASE WHEN :'consumer'='a' THEN '30000000-0000-0000-0000-000000000070' ELSE '30000000-0000-0000-0000-000000000071' END,
        'requested_url','https://example.com/docs','final_url','https://example.com/docs','canonical_url',NULL,
        'title','Concurrent quota fixture','publisher',NULL,'published_at',NULL,'fetched_at',clock_timestamp(),
        'extractor_version','fixture-v1','text',repeat(repeat('x',2048)||E'\n',75),'status','success','error_code',NULL)),repeat('a',32));
SQL
    then printf '%s' ok >"$work_dir/quota-$consumer.status"; else printf '%s' rejected >"$work_dir/quota-$consumer.status"; fi
  ) &
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
if [[ $(cat "$work_dir/quota-a.status") == $(cat "$work_dir/quota-b.status") ]]; then
  cat "$work_dir"/quota-*.log >&2
  exit 1
fi
if ! grep -q 'web_storage_limit' "$work_dir"/quota-*.log; then
  cat "$work_dir"/quota-*.log >&2
  exit 1
fi
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    ASSERT (SELECT count(*) FROM trellis.web_sources WHERE user_id='$fixture_user')=1;
    ASSERT (SELECT retained_bytes=104857600-200000+153675 FROM trellis.web_accounts WHERE user_id='$fixture_user');
    ASSERT (SELECT count(*) FROM trellis.web_reservations WHERE user_id='$fixture_user' AND operation_key LIKE 'budget:%')=1;
    ASSERT (SELECT sum((usage->>'response_bytes')::bigint) FROM trellis.web_runs WHERE user_id='$fixture_user')=41943040;
END \$\$;
SQL
printf '%s\n' 'Concurrent reservations preserved the run ceiling; concurrent sources preserved the owner quota.'
