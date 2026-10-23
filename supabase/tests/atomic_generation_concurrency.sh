#!/usr/bin/env bash
set -euo pipefail
fixture_user=23200000-0000-0000-0000-000000000003
fixture_chat=23200000-0000-0000-0000-000000000025
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
DECLARE r jsonb; c jsonb;
BEGIN
    r:=trellis.mutate('create_run',jsonb_build_object('session_id','$fixture_chat','turn_id','race',
        'client_request_id','race','content','hello','budget_preset','conservative',
        'request_fingerprint',repeat('a',64),
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1)),repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','running','event_type','run.started'),repeat('a',32));
    c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
        'model_id',r#>>'{run,model_id}','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming'),repeat('a',32));
END \$\$;
COMMIT;
SQL
for consumer in a b; do
  psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
SELECT md5(trellis.mutate('commit_generation',jsonb_build_object(
    'run_id',(SELECT id FROM trellis.runs WHERE chat_id='$fixture_chat'),
    'call_id',(SELECT id FROM trellis.model_calls WHERE user_id='$fixture_user'),
    'completion',jsonb_build_object('response_snapshot',jsonb_build_object('content','Answer',
        'tool_calls','[]'::jsonb,'continuation_items','[]'::jsonb,'finish_kind','final'),
        'input_tokens',10,'output_tokens',5,'accounted_total_tokens',15,'estimated_cost',0.01),
    'assistant_message',NULL,'final_content','Answer'),repeat('a',32))::text);
COMMIT;
SQL
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
cmp "$work_dir/a" "$work_dir/b"
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE r trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO r FROM trellis.runs WHERE user_id='$fixture_user';
    ASSERT r.status='completed' AND r.lease_token IS NULL;
    ASSERT (SELECT count(*) FROM trellis.model_calls WHERE run_id=r.id)=1;
    ASSERT (SELECT accounted_total_tokens=15 AND estimated_cost=0.01 FROM trellis.model_calls WHERE run_id=r.id);
    ASSERT (SELECT count(*) FROM trellis.messages WHERE run_id=r.id AND role='assistant')=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='model.completed')=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='assistant.completed')=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='run.completed')=1;
END \$\$;
SQL
printf '%s\n' 'Concurrent generation commits returned one identical receipt and published one final answer.'
