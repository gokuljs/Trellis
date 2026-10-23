#!/usr/bin/env bash
set -euo pipefail
fixture_user=23600000-0000-0000-0000-000000000003
fixture_chat=23600000-0000-0000-0000-000000000075
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
DECLARE r jsonb; c jsonb; a jsonb; preview jsonb:='{"approved":"original"}';
BEGIN
    r:=trellis.mutate('create_run',jsonb_build_object('session_id','$fixture_chat','turn_id','review',
        'client_request_id','review','content','hello','budget_preset','conservative','request_fingerprint',repeat('a',64),
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1)),repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','running','event_type','run.started'),repeat('a',32));
    c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
        'model_id',r#>>'{run,model_id}','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','completed'),repeat('a',32));
    a:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',r#>>'{run,id}','model_call_id',c->>'id',
        'message',jsonb_build_object('role','assistant','content','',
            'tool_calls',jsonb_build_array(jsonb_build_object('id','command-1','name','run_command','arguments','{"command":"example"}'::jsonb)))),repeat('a',32));
    PERFORM trellis.mutate('request_tool_approval',jsonb_build_object('run_id',r#>>'{run,id}',
        'tool_call_id',a#>>'{tool_calls,0,id}','preview',preview),repeat('a',32));
    PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object('run_id',r#>>'{run,id}',
        'tool_call_id',a#>>'{tool_calls,0,id}','decision','approved'),repeat('a',32));
    PERFORM trellis.mutate('resume_approved_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',r#>>'{run,id}',
        'tool_call_id',a#>>'{tool_calls,0,id}','dispatch_id','23600000-0000-0000-0000-000000000751',
        'expected_attempt',0,'recovery_policy','command','input_snapshot',jsonb_build_object(
            'arguments','{"command":"example"}'::jsonb,'approval_preview',preview)),repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','interrupted','event_type','run.interrupted'),repeat('a',32));
END \$\$;
COMMIT;
SQL
for consumer in a b; do
  psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
SELECT md5(trellis.mutate('acknowledge_workspace_review',jsonb_build_object('session_id','$fixture_chat',
    'run_id',(SELECT id FROM trellis.runs WHERE user_id='$fixture_user'),
    'tool_call_id',(SELECT id FROM trellis.tool_calls WHERE user_id='$fixture_user')),NULL)::text);
COMMIT;
SQL
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
cmp "$work_dir/a" "$work_dir/b"
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE t trellis.tool_calls%ROWTYPE;
BEGIN
    SELECT * INTO t FROM trellis.tool_calls WHERE user_id='$fixture_user';
    ASSERT t.status='outcome_unknown' AND t.dispatch_attempt=1 AND t.reviewed_at IS NOT NULL;
    ASSERT (SELECT status='interrupted' AND NOT retry_allowed FROM trellis.runs WHERE id=t.run_id);
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE tool_call_id=t.id);
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=t.run_id AND event_type='run.phase'
        AND data->>'review_acknowledged'='true')=1;
END \$\$;
SQL
printf '%s\n' 'Concurrent manual-review acknowledgments recorded one timestamp and audit event without changing the unknown outcome.'
