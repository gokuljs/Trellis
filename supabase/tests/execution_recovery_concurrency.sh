#!/usr/bin/env bash
set -euo pipefail
fixture_user=23700000-0000-0000-0000-000000000003
fixture_chat=23700000-0000-0000-0000-000000000089
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
    r:=trellis.mutate('create_run',jsonb_build_object('session_id','$fixture_chat','turn_id','race','client_request_id','race',
        'content','hello','budget_preset','conservative','request_fingerprint',repeat('a',64),
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
        'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
            'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions','{}'::jsonb,
            'request_identity','race','installation_id','23700000-0000-0000-0000-000000000099','workspace',NULL)),repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','running','event_type','run.started'),repeat('a',32));
END \$\$;
COMMIT;
SQL
for consumer in a b; do
  psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/allocate-$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
SELECT md5(trellis.mutate('create_model_call',jsonb_build_object(
    'run_id',(SELECT id FROM trellis.runs WHERE user_id='$fixture_user'),'step_index',1,
    'model_id',(SELECT model_id FROM trellis.runs WHERE user_id='$fixture_user'),
    'request_snapshot','{"purpose":"generation","saved":"request"}'::jsonb),repeat('a',32))::text);
COMMIT;
SQL
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
cmp "$work_dir/allocate-a" "$work_dir/allocate-b"
psql -X -q -v ON_ERROR_STOP=1 <<SQL
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
DO \$\$
DECLARE r uuid; c uuid;
BEGIN
    SELECT id INTO r FROM trellis.runs WHERE user_id='$fixture_user';
    SELECT id INTO c FROM trellis.model_calls WHERE run_id=r;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c,'next_status','streaming',
        'input_tokens',21,'output_tokens',7,'accounted_total_tokens',28,'estimated_cost',0.02),repeat('a',32));
    PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',r,'model_call_id',c,
        'batch_index',0,'text','Saved partial output'),repeat('a',32));
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r),repeat('a',32));
    PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',r,'expected_generation',0,
        'installation_id','23700000-0000-0000-0000-000000000099'),repeat('b',32));
END \$\$;
COMMIT;
SQL
for operation in finish stop; do
  for consumer in a b; do
    psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/$operation-$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
SELECT md5(CASE WHEN '$operation'='finish' THEN trellis.mutate('finish_recovery',jsonb_build_object(
    'run_id',(SELECT id FROM trellis.runs WHERE user_id='$fixture_user')),repeat('b',32))
    ELSE trellis.mutate('stop_recovery',jsonb_build_object(
        'run_id',(SELECT id FROM trellis.runs WHERE user_id='$fixture_user'),'reason','model_outcome_unknown',
        'retry_allowed',true,'usage_unknown',true,
        'model_call_id',(SELECT id FROM trellis.model_calls WHERE user_id='$fixture_user'),'tool_call_id',NULL),repeat('b',32)) END::text);
COMMIT;
SQL
    if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
  done
  wait "$first_pid"
  wait "$second_pid"
  cmp "$work_dir/$operation-a" "$work_dir/$operation-b"
done
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE r trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO r FROM trellis.runs WHERE user_id='$fixture_user';
    ASSERT r.status='interrupted' AND r.generation=1 AND r.lease_token IS NULL;
    ASSERT (SELECT count(*) FROM trellis.model_calls WHERE run_id=r.id)=1;
    ASSERT (SELECT status='failed' AND input_tokens=21 AND output_tokens=7
        AND accounted_total_tokens IS NULL AND estimated_cost IS NULL FROM trellis.model_calls WHERE run_id=r.id);
    ASSERT (SELECT status='partial' AND content='Saved partial output' FROM trellis.run_assistant_outputs WHERE run_id=r.id);
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='run.resumed')=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='run.interrupted')=1;
END \$\$;
SQL
printf '%s\n' 'Concurrent model allocation, recovery completion, and unknown-outcome stopping each produced one authoritative result.'
