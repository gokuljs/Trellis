#!/usr/bin/env bash
set -euo pipefail
fixture_user=23300000-0000-0000-0000-000000000003
fixture_chat=23300000-0000-0000-0000-000000000036
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
INSERT INTO trellis.messages(id,user_id,chat_id,turn_id,ordinal,role,content) VALUES
    ('23300000-0000-0000-0000-000000000361','$fixture_user','$fixture_chat','history',1,'user','Old question'),
    ('23300000-0000-0000-0000-000000000362','$fixture_user','$fixture_chat','history',2,'assistant','Old answer');
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
DO \$\$
DECLARE r jsonb; c jsonb; sources jsonb;
BEGIN
    r:=trellis.mutate('create_run',jsonb_build_object('session_id','$fixture_chat','turn_id','race',
        'client_request_id','race','content','hello','budget_preset','conservative',
        'request_fingerprint',repeat('a',64),
        'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1)),repeat('a',32));
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
        'next_status','running','event_type','run.started'),repeat('a',32));
    sources:=jsonb_build_array(
        jsonb_build_object('source_kind','message','source_id','23300000-0000-0000-0000-000000000361','turn_id','history'),
        jsonb_build_object('source_kind','message','source_id','23300000-0000-0000-0000-000000000362','turn_id','history'));
    c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
        'model_id',r#>>'{run,model_id}','request_snapshot',jsonb_build_object('purpose','summary',
            'compaction_intent',jsonb_build_object('version',1,'operation_id','23300000-0000-0000-0000-000000000364',
                'target_checkpoint_id','23300000-0000-0000-0000-000000000363','expected_revision',0,
                'parent_checkpoint_id',NULL,'sources',sources,'source_hash',repeat('a',64),'input_hash',repeat('b',64),
                'instruction_hash',repeat('c',64),'workspace_hash',repeat('d',64),'input_tokens_before',100,
                'pricing_context_tokens',0,'native_model_call_id',NULL,'prefix_length',2,
                'generation_plan','{}'::jsonb,'summary_plan','{}'::jsonb))),repeat('a',32));
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming'),repeat('a',32));
END \$\$;
COMMIT;
SQL
for consumer in a b; do
  psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
WITH c AS (SELECT id,run_id,request_snapshot->'compaction_intent' AS intent FROM trellis.model_calls WHERE user_id='$fixture_user')
SELECT md5(trellis.mutate('commit_summary',jsonb_build_object('run_id',c.run_id,'call_id',c.id,
    'completion',jsonb_build_object('response_snapshot',jsonb_build_object('content','Saved summary'),
        'input_tokens',10,'output_tokens',5,'accounted_total_tokens',15,'estimated_cost',0.01),
    'expected_revision',0,'draft',jsonb_build_object('id',c.intent->>'target_checkpoint_id','model_call_id',c.id,
        'sources',c.intent->'sources','summary','Saved summary','source_hash',repeat('a',64),'input_hash',repeat('b',64),
        'instruction_hash',repeat('c',64),'workspace_hash',repeat('d',64),'input_tokens_before',100,
        'input_tokens_after',20,'pricing_context_tokens',0,'native_kind',NULL,'native_payload_json',NULL)),repeat('a',32))::text)
FROM c;
COMMIT;
SQL
  if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
cmp "$work_dir/a" "$work_dir/b"
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE r uuid;
BEGIN
    SELECT id INTO r FROM trellis.runs WHERE user_id='$fixture_user';
    ASSERT (SELECT count(*) FROM trellis.model_calls WHERE run_id=r AND status='completed')=1;
    ASSERT (SELECT accounted_total_tokens=15 AND estimated_cost=0.01 FROM trellis.model_calls WHERE run_id=r);
    ASSERT (SELECT count(*) FROM trellis.context_checkpoints WHERE run_id=r)=1;
    ASSERT (SELECT context_checkpoint_revision=1 FROM trellis.chats WHERE id='$fixture_chat');
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r AND event_type='model.completed')=1;
END \$\$;
SQL
printf '%s\n' 'Concurrent summary commits returned one identical receipt and published one checkpoint revision.'
