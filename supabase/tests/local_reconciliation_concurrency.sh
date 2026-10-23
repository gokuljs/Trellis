#!/usr/bin/env bash
set -euo pipefail
fixture_user=23500000-0000-0000-0000-000000000003
fixture_chat=23500000-0000-0000-0000-000000000060
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
DECLARE r jsonb; c jsonb; a jsonb;
BEGIN
    r:=trellis.mutate('create_run',jsonb_build_object('session_id','$fixture_chat','turn_id','race','client_request_id','race',
        'content','hello','budget_preset','conservative','request_fingerprint',repeat('a',64),
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
        'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
            'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object('read_file','v1'),
            'request_identity','race','installation_id','23500000-0000-0000-0000-000000000099','workspace',NULL)),repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','running','event_type','run.started'),repeat('a',32));
    c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
        'model_id',r#>>'{run,model_id}','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','completed'),repeat('a',32));
    a:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',r#>>'{run,id}','model_call_id',c->>'id',
        'message',jsonb_build_object('role','assistant','content','',
            'tool_calls',jsonb_build_array(jsonb_build_object('id','read-1','name','read_file','arguments','{"path":"a.py"}'::jsonb)))),repeat('a',32));
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',r#>>'{run,id}','tool_call_id',a#>>'{tool_calls,0,id}',
        'dispatch_id','23500000-0000-0000-0000-000000000601','expected_attempt',0,'recovery_policy','read_only','input_snapshot',NULL),repeat('a',32));
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
    PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}','expected_generation',0,
        'installation_id','23500000-0000-0000-0000-000000000099'),repeat('b',32));
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('b',32));
END \$\$;
COMMIT;
SQL
for operation in authorize dispatch; do
  for consumer in a b; do
    psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/$operation-$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
WITH t AS (SELECT id,run_id FROM trellis.tool_calls WHERE user_id='$fixture_user')
SELECT md5(CASE WHEN '$operation'='authorize' THEN
    trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',t.run_id,'tool_call_id',t.id,
        'expected_dispatch_id','23500000-0000-0000-0000-000000000601','expected_attempt',1,
        'reconciliation','read_retry','proof',NULL),repeat('b',32))
    ELSE trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',t.run_id,'tool_call_id',t.id,
        'dispatch_id','23500000-0000-0000-0000-000000000602','expected_attempt',1,'recovery_policy','read_only','input_snapshot',NULL),repeat('b',32)) END::text)
FROM t;
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
DECLARE t trellis.tool_calls%ROWTYPE;
BEGIN
    SELECT * INTO t FROM trellis.tool_calls WHERE user_id='$fixture_user';
    ASSERT t.status='running' AND t.dispatch_attempt=2 AND t.dispatch_generation=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=t.run_id AND event_type='run.phase'
        AND data->>'reconciliation'='read_retry')=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=t.run_id AND event_type='run.phase'
        AND data->>'dispatch_id'='23500000-0000-0000-0000-000000000602')=1;
END \$\$;
SQL
printf '%s\n' 'Concurrent retry authorization and dispatch allocated one bounded replacement attempt.'
