#!/usr/bin/env bash
set -euo pipefail

if [[ ${PGDATABASE:-} != trellis_test && ${PGDATABASE:-} != trellis_stage7_test ]]; then
  printf '%s\n' 'Run this fixture only against a disposable trellis_test or trellis_stage7_test database.' >&2
  exit 1
fi

fixture_user_a=33000000-0000-0000-0000-000000000003
fixture_user_b=33000000-0000-0000-0000-000000000004
fixture_chat_a=33000000-0000-0000-0000-000000000012
fixture_chat_b=33000000-0000-0000-0000-000000000013
fixture_chat_c=33000000-0000-0000-0000-000000000014
fixture_origin="https://crawl-concurrency-${RANDOM}.example"
work_dir=$(mktemp -d)
cleanup() {
  psql -X -q -v ON_ERROR_STOP=1 -v origin="$fixture_origin" <<SQL >/dev/null
DELETE FROM trellis.web_crawls WHERE user_id IN ('$fixture_user_a','$fixture_user_b');
DELETE FROM auth.users WHERE id IN ('$fixture_user_a','$fixture_user_b');
DELETE FROM trellis_private.web_crawl_origins WHERE origin_hash=encode(sha256(convert_to(:'origin','UTF8')),'hex');
SQL
  rm -rf "$work_dir"
}
trap cleanup EXIT

psql -X -q -v ON_ERROR_STOP=1 <<SQL
INSERT INTO auth.users(id) VALUES ('$fixture_user_a'),('$fixture_user_b');
INSERT INTO trellis.profiles(id) VALUES ('$fixture_user_a'),('$fixture_user_b');
INSERT INTO trellis.chats(id,user_id) VALUES ('$fixture_chat_a','$fixture_user_a'),
    ('$fixture_chat_b','$fixture_user_b'),('$fixture_chat_c','$fixture_user_a');
SQL
sed -n '/^CREATE FUNCTION pg_temp.web_fixture(/,/^\$\$;/p' supabase/tests/web_crawl.sql >"$work_dir/setup.sql"
cat >>"$work_dir/setup.sql" <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user_a';
SELECT pg_temp.web_fixture('$fixture_chat_a','crawl-concurrent-a') AS scope \gset a_
SELECT pg_temp.web_fixture('$fixture_chat_c','crawl-concurrent-c') AS scope \gset c_
SET request.jwt.claim.sub='$fixture_user_b';
SELECT pg_temp.web_fixture('$fixture_chat_b','crawl-concurrent-b') AS scope \gset b_
RESET ROLE;
SELECT jsonb_build_object('a',:'a_scope'::jsonb,'b',:'b_scope'::jsonb,'c',:'c_scope'::jsonb);
SQL
psql -X -qAt -v ON_ERROR_STOP=1 -f "$work_dir/setup.sql" >"$work_dir/scopes.json"
for consumer in a b c; do
  if [[ $consumer == b ]]; then fixture_user=$fixture_user_b; else fixture_user=$fixture_user_a; fi
  scope_value=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))[sys.argv[2]]))' "$work_dir/scopes.json" "$consumer")
  psql -X -qAt -v ON_ERROR_STOP=1 -v scope="$scope_value" -v origin="$fixture_origin" >"$work_dir/page-$consumer.json" <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user';
SELECT trellis.web_mutate('open_run',:'scope'::jsonb,repeat('a',32)) AS state \gset opened_
SELECT gen_random_uuid() AS crawl_id,gen_random_uuid() AS reservation_id,gen_random_uuid() AS source_id \gset page_
SELECT trellis.web_mutate('create_crawl',:'scope'::jsonb||jsonb_build_object('crawl_id',:'page_crawl_id',
    'seed_url',:'origin'||'/docs','origin',:'origin','path_prefix','/docs','max_pages',1,'max_depth',1),repeat('a',32)) AS state \gset created_
SELECT trellis.web_mutate('enqueue_pages',:'scope'::jsonb||jsonb_build_object('crawl_id',:'page_crawl_id',
    'pages',jsonb_build_array(jsonb_build_object('url',:'origin'||'/docs','depth',0))),repeat('a',32)) AS state \gset queued_
SELECT trellis.web_mutate('claim_page',:'scope'::jsonb||jsonb_build_object('crawl_id',:'page_crawl_id'),repeat('a',32)) AS state \gset claimed_
SELECT trellis.web_mutate('reserve',:'scope'::jsonb||jsonb_build_object('reservation_id',:'page_reservation_id','operation_key','crawl-page',
    'usage','{"search_calls":0,"page_attempts":1,"network_requests":7,"response_bytes":18874368,"extracted_bytes":262144}'::jsonb),repeat('a',32)) AS state \gset reserved_
SELECT :'scope'::jsonb||jsonb_build_object('crawl_id',:'page_crawl_id','url',:'origin'||'/docs','page_attempt',1,
    'reservation_id',:'page_reservation_id','source',jsonb_build_object('id',:'page_source_id','requested_url',:'origin'||'/docs',
        'final_url',:'origin'||'/docs','canonical_url',NULL,'title',NULL,'publisher',NULL,'published_at',NULL,
        'fetched_at','2026-10-30T00:00:00Z','extractor_version','fixture-v1','text','Facts.','status','success','error_code',NULL),
    'usage','{"search_calls":0,"page_attempts":1,"network_requests":2,"response_bytes":100,"extracted_bytes":6}'::jsonb,
    'discovered_links',jsonb_build_array(:'origin'||'/docs/next'));
SQL
done

pace() {
  local consumer=$1
  local output=$2
  local fixture_user=$fixture_user_a
  if [[ $consumer == b ]]; then fixture_user=$fixture_user_b; fi
  psql -X -qAt -v ON_ERROR_STOP=1 -v page="$(cat "$work_dir/page-$consumer.json")" >"$output" <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user';
SELECT trellis.web_mutate('reserve_crawl_origin',:'page'::jsonb,repeat('a',32));
SQL
}
pace a "$work_dir/pace-a.json" &
first_pid=$!
pace b "$work_dir/pace-b.json" &
second_pid=$!
wait "$first_pid"
wait "$second_pid"
winner=$(python3 - "$work_dir" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
values = {name: json.loads((root / f"pace-{name}.json").read_text())["wait_seconds"] for name in ("a", "b")}
assert sum(value == 0 for value in values.values()) == 1, values
assert all(0 <= value <= 1 for value in values.values()), values
print(next(name for name, value in values.items() if value == 0))
PY
)
if [[ $winner == a ]]; then loser=b; else loser=a; fi
pace "$winner" "$work_dir/pace-replay.json"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1])) == {"wait_seconds":0}' "$work_dir/pace-replay.json"
psql -X -q -v ON_ERROR_STOP=1 -c 'SELECT pg_sleep(1.05)' >/dev/null
pace "$loser" "$work_dir/pace-loser.json"
pace "$winner" "$work_dir/pace-lost-reply.json"
python3 - "$work_dir" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
assert json.loads((root / "pace-loser.json").read_text()) == {"wait_seconds": 0}
assert 0 < json.loads((root / "pace-lost-reply.json").read_text())["wait_seconds"] <= 1
PY
psql -X -q -v ON_ERROR_STOP=1 -v origin="$fixture_origin" <<SQL
SELECT set_config('fixture.origin',:'origin',false) AS fixture_state \gset
DO \$\$
DECLARE v_first timestamptz; v_second timestamptz;
BEGIN
    SELECT min(granted_at),max(granted_at) INTO v_first,v_second FROM trellis_private.web_crawl_origin_attempts
        WHERE origin_hash=encode(sha256(convert_to(current_setting('fixture.origin'),'UTF8')),'hex');
    ASSERT v_second-v_first>=interval '1 second','different accounts bypassed origin spacing';
END;
\$\$;
UPDATE trellis.web_accounts SET retained_bytes=104857594 WHERE user_id='$fixture_user_a';
SQL

psql -X -q -v ON_ERROR_STOP=1 -v page="$(cat "$work_dir/page-b.json")" <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user_b';
SELECT set_config('fixture.page',:'page',false) AS fixture_state \gset
DO \$\$
DECLARE v_page jsonb:=current_setting('fixture.page')::jsonb; v_i integer;
BEGIN
    FOR v_i IN 1..18 LOOP
        PERFORM trellis.web_mutate('reserve',v_page||jsonb_build_object('reservation_id',gen_random_uuid(),
            'operation_key','page-attempt:'||v_i,'usage',
            '{"search_calls":0,"page_attempts":1,"network_requests":0,"response_bytes":0,"extracted_bytes":0}'::jsonb),repeat('a',32));
    END LOOP;
END;
\$\$;
SQL
for consumer in 20 21; do
  (
    if psql -X -qAt -v ON_ERROR_STOP=1 -v page="$(cat "$work_dir/page-b.json")" -v consumer="$consumer" >"$work_dir/attempt-$consumer.log" 2>&1 <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user_b';
SELECT trellis.web_mutate('reserve',:'page'::jsonb||jsonb_build_object('reservation_id',gen_random_uuid(),
    'operation_key','page-attempt:'||:'consumer','usage',
    '{"search_calls":0,"page_attempts":1,"network_requests":0,"response_bytes":0,"extracted_bytes":0}'::jsonb),repeat('a',32));
SQL
    then printf '%s' ok >"$work_dir/attempt-$consumer.status"; else printf '%s' rejected >"$work_dir/attempt-$consumer.status"; fi
  ) &
  if [[ $consumer == 20 ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
if [[ $(cat "$work_dir/attempt-20.status") == $(cat "$work_dir/attempt-21.status") ]]; then
  cat "$work_dir"/attempt-*.log >&2
  exit 1
fi
if ! grep -q 'web_budget_exhausted' "$work_dir"/attempt-*.log; then
  cat "$work_dir"/attempt-*.log >&2
  exit 1
fi

for consumer in a c; do
  (
    if psql -X -qAt -v ON_ERROR_STOP=1 -v page="$(cat "$work_dir/page-$consumer.json")" >"$work_dir/quota-$consumer.log" 2>&1 <<SQL
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user_a';
SELECT trellis.web_mutate('finalize_crawl_page',:'page'::jsonb,repeat('a',32));
SQL
    then printf '%s' ok >"$work_dir/quota-$consumer.status"; else printf '%s' rejected >"$work_dir/quota-$consumer.status"; fi
  ) &
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
if [[ $(cat "$work_dir/quota-a.status") == $(cat "$work_dir/quota-c.status") ]]; then
  cat "$work_dir"/quota-*.log >&2
  exit 1
fi
if ! grep -q 'web_storage_limit' "$work_dir"/quota-*.log; then
  cat "$work_dir"/quota-*.log >&2
  exit 1
fi
if [[ $(cat "$work_dir/quota-a.status") == ok ]]; then completed=a; else completed=c; fi
for replay in 1 2; do
  psql -X -qAt -v ON_ERROR_STOP=1 -v page="$(cat "$work_dir/page-$completed.json")" >"$work_dir/replay-$replay.json" <<SQL &
SET ROLE authenticated;
SET request.jwt.claim.sub='$fixture_user_a';
SELECT trellis.web_mutate('finalize_crawl_page',:'page'::jsonb,repeat('a',32));
SQL
  if [[ $replay == 1 ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
cmp "$work_dir/replay-1.json" "$work_dir/replay-2.json"
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    ASSERT (SELECT count(*)=1 FROM trellis.web_sources WHERE user_id='$fixture_user_a');
    ASSERT (SELECT count(*)=1 FROM trellis.web_reservations WHERE user_id='$fixture_user_a' AND status='settled');
    ASSERT (SELECT count(*)=1 FROM trellis.web_crawl_pages WHERE user_id='$fixture_user_a' AND status='completed');
    ASSERT (SELECT count(*)=1 FROM trellis.web_crawl_pages WHERE user_id='$fixture_user_a' AND status='fetching' AND source_id IS NULL);
    ASSERT (SELECT count(*)=1 FROM trellis.web_crawl_pages WHERE user_id='$fixture_user_a' AND status='queued');
    ASSERT (SELECT sum((usage->>'response_bytes')::integer)=18874468 FROM trellis.web_runs WHERE user_id='$fixture_user_a');
    ASSERT (SELECT retained_bytes=104857600 FROM trellis.web_accounts WHERE user_id='$fixture_user_a');
    ASSERT (SELECT (usage->>'page_attempts')::integer=20 AND reservation_count=20
        FROM trellis.web_runs WHERE user_id='$fixture_user_b');
END;
\$\$;
SQL
printf '%s\n' 'Concurrent crawls preserved global origin spacing, lost-reply pacing, the 20 attempt ceiling, atomic quota settlement, and immutable completion replay.'
