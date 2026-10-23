#!/usr/bin/env bash
set -euo pipefail
fixture_user=23000000-0000-0000-0000-000000000003
fixture_chat=23000000-0000-0000-0000-000000000017
work_dir=$(mktemp -d)
cleanup() {
  psql -X -q -v ON_ERROR_STOP=1 -c "DELETE FROM auth.users WHERE id='$fixture_user'" >/dev/null
  rm -rf "$work_dir"
}
trap cleanup EXIT
psql -X -q -v ON_ERROR_STOP=1 <<SQL
INSERT INTO auth.users(id) VALUES ('$fixture_user');
INSERT INTO trellis.profiles(id) VALUES ('$fixture_user');
INSERT INTO trellis.chats(id,user_id) VALUES ('$fixture_chat','$fixture_user');
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
DO \$\$
DECLARE r jsonb;
BEGIN
    r:=trellis.mutate('create_run',jsonb_build_object(
        'session_id','$fixture_chat','turn_id','race','client_request_id','race','content','hello',
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
        'budget_preset','conservative','request_fingerprint',repeat('a',64),
        'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
            'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions','{}'::jsonb,
            'request_identity','race','installation_id','23000000-0000-0000-0000-000000000099',
            'workspace',NULL)),repeat('a',32));
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
END \$\$;
COMMIT;
SQL
for consumer in b c; do
  psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
SELECT trellis.mutate('claim_recovery',jsonb_build_object(
    'run_id',(SELECT id FROM trellis.runs WHERE chat_id='$fixture_chat'),
    'installation_id','23000000-0000-0000-0000-000000000099','expected_generation',0),repeat('$consumer',32))
    ->>'lease_acquired';
SELECT pg_sleep(0.1);
COMMIT;
SQL
  if [[ $consumer == b ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE r trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO r FROM trellis.runs WHERE user_id='$fixture_user';
    ASSERT r.generation=1 AND r.recovery_count=1 AND r.status='recovering','takeover was duplicated';
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='run.recovering')=1,
        'takeover produced duplicate lifecycle events';
END \$\$;
SQL
if [[ $(awk '/^true$/ {n++} END {print n+0}' "$work_dir/b" "$work_dir/c") != 1 ]]; then
  echo 'Concurrent claim did not produce exactly one owner.' >&2
  exit 1
fi
printf '%s\n' 'Concurrent recovery claims produced one owner, generation, and lifecycle event.'
