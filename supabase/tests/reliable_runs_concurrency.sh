#!/usr/bin/env bash
set -euo pipefail
fixture_user=19000000-0000-0000-0000-000000000003
fixture_chat=19000000-0000-0000-0000-000000000012
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
SQL
for consumer in a b; do
  psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
SELECT trellis.mutate('create_run',jsonb_build_object(
    'session_id','$fixture_chat','turn_id','same','client_request_id','same','content','hello',
    'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
    'budget_preset','conservative','request_fingerprint',repeat('a',64)),repeat('$consumer',32))
    ->'run'->>'id';
SELECT pg_sleep(0.1);
COMMIT;
SQL
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE r uuid;
BEGIN
    ASSERT (SELECT count(*) FROM trellis.runs WHERE user_id='$fixture_user')=1,'duplicate run';
    ASSERT (SELECT count(*) FROM trellis.messages WHERE user_id='$fixture_user')=1,'duplicate input';
    SELECT id INTO r FROM trellis.runs WHERE user_id='$fixture_user';
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r)=1,'duplicate queued event';
END \$\$;
SQL
printf '%s\n' 'Concurrent identical requests created one run, input message, and queued event.'
