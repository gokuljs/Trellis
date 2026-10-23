#!/usr/bin/env bash
set -euo pipefail
fixture_user=23800000-0000-0000-0000-000000000003
work_dir=$(mktemp -d)
cleanup() {
  psql -X -q -v ON_ERROR_STOP=1 -c "DELETE FROM auth.users WHERE id='$fixture_user'" >/dev/null
  rm -rf "$work_dir"
}
trap cleanup EXIT
psql -X -q -v ON_ERROR_STOP=1 <<SQL
INSERT INTO auth.users(id) VALUES ('$fixture_user');
INSERT INTO trellis.profiles(id) VALUES ('$fixture_user');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23800000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,'$fixture_user'::uuid FROM generate_series(180,182) n;
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
DO \$\$
DECLARE r jsonb; c jsonb;
BEGIN
    FOR n IN 180..182 LOOP
        r:=trellis.mutate('create_run',jsonb_build_object('session_id','23800000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','race','client_request_id','race','content','Saved request','budget_preset','conservative',
            'request_fingerprint',repeat('a',64),'model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions','{}'::jsonb,
                'request_identity','race','installation_id','23800000-0000-0000-0000-000000000099','workspace',NULL)),repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        IF n=180 THEN
            c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}',
                'step_index',1,'model_id',r#>>'{run,model_id}','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
            PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming',
                'input_tokens',11,'output_tokens',3,'accounted_total_tokens',14,'estimated_cost',0.01),repeat('a',32));
            PERFORM trellis.mutate('append_assistant_batch',jsonb_build_object('run_id',r#>>'{run,id}',
                'model_call_id',c->>'id','batch_index',0,'text','Preserved partial response'),repeat('a',32));
        END IF;
        IF n<>182 THEN PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32)); END IF;
    END LOOP;
END \$\$;
COMMIT;
SQL
for operation in reject stop; do
  for consumer in a b; do
    psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/$operation-$consumer" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
SELECT md5(CASE WHEN '$operation'='reject' THEN trellis.mutate('claim_recovery',jsonb_build_object(
    'run_id',(SELECT id FROM trellis.runs WHERE chat_id='23800000-0000-0000-0000-000000000180'),
    'installation_id','foreign','expected_generation',0),NULL)
    ELSE trellis.mutate('request_recovery_cancellation',jsonb_build_object(
        'run_id',(SELECT id FROM trellis.runs WHERE chat_id='23800000-0000-0000-0000-000000000182')),NULL)->'run' END::text);
COMMIT;
SQL
    if [[ $consumer == a ]]; then first_pid=$!; else second_pid=$!; fi
  done
  wait "$first_pid"
  wait "$second_pid"
  cmp "$work_dir/$operation-a" "$work_dir/$operation-b"
done
for operation in claim stop; do
  psql -X -qAt -v ON_ERROR_STOP=1 >"$work_dir/race-$operation" <<SQL &
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
SELECT CASE WHEN '$operation'='claim' THEN trellis.mutate('claim_recovery',jsonb_build_object(
    'run_id',(SELECT id FROM trellis.runs WHERE chat_id='23800000-0000-0000-0000-000000000181'),
    'installation_id','23800000-0000-0000-0000-000000000099','expected_generation',0),repeat('b',32))
    ELSE trellis.mutate('request_recovery_cancellation',jsonb_build_object(
        'run_id',(SELECT id FROM trellis.runs WHERE chat_id='23800000-0000-0000-0000-000000000181')),NULL) END;
COMMIT;
SQL
  if [[ $operation == claim ]]; then first_pid=$!; else second_pid=$!; fi
done
wait "$first_pid"
wait "$second_pid"
psql -X -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE r trellis.runs%ROWTYPE;
BEGIN
    SELECT * INTO r FROM trellis.runs WHERE chat_id='23800000-0000-0000-0000-000000000180';
    ASSERT r.status='interrupted' AND r.recovery_reason='different_installation'
        AND r.generation=0 AND r.recovery_count=0 AND r.lease_token IS NULL;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='run.interrupted')=1;
    ASSERT (SELECT status='failed' AND input_tokens=11 AND output_tokens=3
        AND accounted_total_tokens IS NULL AND estimated_cost IS NULL FROM trellis.model_calls WHERE run_id=r.id);
    ASSERT (SELECT status='partial' AND content='Preserved partial response' FROM trellis.run_assistant_outputs WHERE run_id=r.id);
    SELECT * INTO r FROM trellis.runs WHERE chat_id='23800000-0000-0000-0000-000000000182';
    ASSERT r.status='cancelling' AND r.cancel_requested_at IS NOT NULL AND r.generation=0 AND r.lease_token IS NOT NULL;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='run.cancellation_requested')=1;
    SELECT * INTO r FROM trellis.runs WHERE chat_id='23800000-0000-0000-0000-000000000181';
    ASSERT r.status='cancelling' AND r.cancel_requested_at IS NOT NULL AND r.generation=1 AND r.recovery_count=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='run.cancellation_requested')=1;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=r.id AND event_type='run.recovering')=1;
END \$\$;
BEGIN;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','$fixture_user',true);
DO \$\$
DECLARE r uuid;
BEGIN
    SELECT id INTO r FROM trellis.runs WHERE chat_id='23800000-0000-0000-0000-000000000181';
    BEGIN
        PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',r),repeat('b',32));
        RAISE EXCEPTION 'a racing Stop intent allowed execution to resume';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    ASSERT trellis.mutate('stop_recovery',jsonb_build_object('run_id',r,'reason','cancellation_requested',
        'retry_allowed',true,'usage_unknown',false,'model_call_id',NULL,'tool_call_id',NULL),repeat('b',32))#>>'{run,status}'='cancelled';
END \$\$;
COMMIT;
SQL
printf '%s\n' 'Concurrent foreign-policy rejection and Stop intents are idempotent; Stop racing takeover prevents execution.'
