\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES ('30000000-0000-0000-0000-000000000001'),('30000000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES ('30000000-0000-0000-0000-000000000001'),('30000000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) VALUES
    ('30000000-0000-0000-0000-000000000010','30000000-0000-0000-0000-000000000001'),
    ('30000000-0000-0000-0000-000000000011','30000000-0000-0000-0000-000000000001');

CREATE FUNCTION pg_temp.web_fixture(p_chat uuid,p_turn text,p_tool text DEFAULT 'web_crawl') RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE v_run uuid; v_call uuid; v_tool uuid; v_result jsonb; v_dispatch uuid:=gen_random_uuid();
    v_model text:=(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1);
BEGIN
    v_result:=trellis.mutate('create_run',jsonb_build_object('session_id',p_chat,'turn_id',p_turn,
        'client_request_id',p_turn,'content','Read public evidence.','model_id',v_model,
        'budget_preset','conservative','request_fingerprint',repeat('a',64),
        'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
            'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object(p_tool,'v1'),
            'request_identity',p_turn,'installation_id','30000000-0000-0000-0000-000000000099','workspace',NULL)),repeat('a',32));
    v_run:=(v_result->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,'next_status','running','event_type','run.started'),repeat('a',32));
    v_result:=trellis.mutate('create_model_call',jsonb_build_object('run_id',v_run,'step_index',1,
        'model_id',v_model,'request_snapshot','{}'::jsonb),repeat('a',32));
    v_call:=(v_result->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',v_call,'next_status','streaming'),repeat('a',32));
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',v_call,'next_status','completed'),repeat('a',32));
    v_result:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',v_run,'model_call_id',v_call,
        'message',jsonb_build_object('role','assistant','content','Read the page.','tool_calls',jsonb_build_array(
            jsonb_build_object('id','fixture-call','name',p_tool,'arguments','{}'::jsonb)))),repeat('a',32));
    v_tool:=(v_result->'tool_calls'->0->>'id')::uuid;
    IF p_tool='run_command' THEN
        PERFORM trellis.mutate('request_tool_approval',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,'preview','{}'::jsonb),repeat('a',32));
        PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,'decision','approved'),repeat('a',32));
        PERFORM trellis.mutate('resume_approved_run',jsonb_build_object('run_id',v_run),repeat('a',32));
    END IF;
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
        'dispatch_id',v_dispatch,'expected_attempt',0,'recovery_policy',CASE WHEN p_tool='run_command' THEN 'command' ELSE 'checkpointed' END,
        'input_snapshot',NULL),repeat('a',32));
    RETURN jsonb_build_object('user_id',auth.uid(),'session_id',p_chat,'run_id',v_run,'tool_call_id',v_tool,
        'dispatch_id',v_dispatch,'dispatch_attempt',1,'generation',0,'evidence_epoch',0,'model_call_id',v_call);
END;
$$;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','30000000-0000-0000-0000-000000000001',true);
DO $$
DECLARE v_scope jsonb; v_old_scope jsonb; v_source jsonb; v_result jsonb; v_again jsonb; v_crawl jsonb;
    v_usage jsonb:='{"search_calls":1,"page_attempts":1,"network_requests":2,"response_bytes":4096,"extracted_bytes":100}';
    v_actual jsonb; v_text text:=E'First line\nSecond line'; v_run uuid; v_call uuid; v_tool uuid;
    v_reservation uuid:='30000000-0000-0000-0000-000000000020';
    v_source_id uuid:='30000000-0000-0000-0000-000000000030';
    v_crawl_id uuid:='30000000-0000-0000-0000-000000000040'; v_page jsonb; v_dispatch uuid;
    v_attempt integer; v_capability text; v_next_capability text;
BEGIN
    v_scope:=pg_temp.web_fixture('30000000-0000-0000-0000-000000000010','web-evidence');
    v_run:=(v_scope->>'run_id')::uuid; v_call:=(v_scope->>'model_call_id')::uuid; v_tool:=(v_scope->>'tool_call_id')::uuid;
    v_scope:=v_scope-'model_call_id';
    ASSERT trellis.web_mutate('open_run',v_scope,repeat('a',32))->>'evidence_epoch'='0';
    ASSERT trellis.web_mutate('open_run',v_scope,repeat('a',32))->'usage'->>'page_attempts'='0';
    BEGIN
        PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_reservation,'operation_key','empty-operation',
            'usage','{"search_calls":0,"page_attempts":0,"network_requests":0,"response_bytes":100,"extracted_bytes":0}'::jsonb),repeat('a',32));
        RAISE EXCEPTION 'an empty operation created a reservation';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_scope_invalid'; END;
    BEGIN
        PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_reservation,'operation_key','page:1',
            'usage',v_usage||'{"page_attempts":21}'),repeat('a',32));
        RAISE EXCEPTION 'page ceiling was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_budget_exhausted'; END;
    ASSERT (SELECT usage->>'page_attempts'='0' FROM trellis.web_runs WHERE run_id=v_run);
    v_result:=trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_reservation,'operation_key','page:1','usage',v_usage),repeat('a',32));
    ASSERT v_result=trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_reservation,'operation_key','page:1','usage',v_usage),repeat('a',32));
    ASSERT (SELECT usage->>'page_attempts'='1' FROM trellis.web_runs WHERE run_id=v_run),'reservation replay charged twice';
    BEGIN
        PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',gen_random_uuid(),'operation_key','page:1','usage',v_usage),repeat('a',32));
        RAISE EXCEPTION 'changed reservation identity was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_source_conflict'; END;
    BEGIN
        PERFORM trellis.web_mutate('record_source',v_scope||jsonb_build_object('reservation_id',v_reservation,
            'source',v_source||jsonb_build_object('text',repeat('x',2049))),repeat('a',32));
        RAISE EXCEPTION 'unbounded source line was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_scope_invalid'; END;
    v_actual:=v_usage||jsonb_build_object('network_requests',1,'response_bytes',100,'extracted_bytes',octet_length(v_text));
    BEGIN
        PERFORM trellis.web_mutate('settle',v_scope||jsonb_build_object('reservation_id',v_reservation,'usage',v_actual),repeat('a',32));
        RAISE EXCEPTION 'extraction charge settled before source was saved';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_source_conflict'; END;
    v_source:=jsonb_build_object('id',v_source_id,'requested_url','https://example.com/docs','final_url','https://example.com/docs',
        'canonical_url','https://example.com/docs','title','Fixture evidence','publisher','Fixture','published_at',NULL,
        'fetched_at','2026-10-29T00:00:00Z','extractor_version','html-v1','text',v_text,'status','success','error_code',NULL);
    v_result:=trellis.web_mutate('record_source',v_scope||jsonb_build_object('reservation_id',v_reservation,'source',v_source),repeat('a',32));
    ASSERT v_result->>'content_hash'=encode(sha256(convert_to(v_text,'UTF8')),'hex');
    ASSERT (v_result->>'line_count')::integer=2 AND (v_result->>'byte_count')::integer=octet_length(v_text);
    ASSERT NOT v_result ? 'text' AND NOT v_result ? 'reservation_id';
    ASSERT v_result=trellis.web_mutate('record_source',v_scope||jsonb_build_object('reservation_id',v_reservation,'source',v_source),repeat('a',32));
    BEGIN
        PERFORM trellis.web_mutate('record_source',v_scope||jsonb_build_object('reservation_id',v_reservation,
            'source',v_source||'{"text":"Changed evidence"}'),repeat('a',32));
        RAISE EXCEPTION 'immutable source changed';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_source_conflict'; END;
    v_result:=trellis.web_mutate('settle',v_scope||jsonb_build_object('reservation_id',v_reservation,'usage',v_actual),repeat('a',32));
    ASSERT v_result=trellis.web_mutate('settle',v_scope||jsonb_build_object('reservation_id',v_reservation,'usage',v_actual),repeat('a',32));
    ASSERT (SELECT usage=v_actual FROM trellis.web_runs WHERE run_id=v_run),'settlement did not preserve actual charge';
    ASSERT (SELECT retained_bytes=octet_length(v_text) FROM trellis.web_accounts WHERE user_id=auth.uid());
    v_result:=trellis.web_mutate('read_source',jsonb_build_object('session_id',v_scope->>'session_id','source_id',v_source_id,'start_line',2,'max_lines',1));
    ASSERT v_result->'lines'='["Second line"]'::jsonb AND v_result->>'total_lines'='2';
    ASSERT trellis.web_mutate('read_source',jsonb_build_object('session_id','30000000-0000-0000-0000-000000000011',
        'source_id',v_source_id,'start_line',1,'max_lines',200)) IS NULL,'another conversation read evidence';
    ASSERT jsonb_array_length(trellis.web_mutate('list_sources',jsonb_build_object('session_id',v_scope->>'session_id','offset',0,'limit',20))->'items')=1;
    BEGIN
        PERFORM trellis.web_mutate('clear_sources',jsonb_build_object('session_id',v_scope->>'session_id'));
        RAISE EXCEPTION 'active sources were cleared';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_sources_busy'; END;
    BEGIN
        UPDATE trellis.web_sources SET title='Changed' WHERE id=v_source_id;
        RAISE EXCEPTION 'direct source writes allowed';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;

    v_crawl:=v_scope||jsonb_build_object('crawl_id',v_crawl_id,'seed_url','https://example.com/docs','origin','https://example.com',
        'path_prefix','/docs','max_pages',2,'max_depth',2);
    v_result:=trellis.web_mutate('create_crawl',v_crawl,repeat('a',32));
    ASSERT v_result=trellis.web_mutate('create_crawl',v_crawl,repeat('a',32));
    BEGIN
        PERFORM trellis.web_mutate('create_crawl',v_crawl||jsonb_build_object('crawl_id',gen_random_uuid()),repeat('a',32));
        RAISE EXCEPTION 'one tool created multiple crawl records';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_source_conflict'; END;
    ASSERT trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl_id,'pages',jsonb_build_array(
        jsonb_build_object('url','https://example.com/docs','depth',0),jsonb_build_object('url','https://example.com/docs/next','depth',1),
        jsonb_build_object('url','https://example.com/docs','depth',0))),repeat('a',32))->>'enqueued_count'='2';
    BEGIN
        PERFORM trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl_id,'pages',
            jsonb_build_array(jsonb_build_object('url','https://another.example/docs','depth',1))),repeat('a',32));
        RAISE EXCEPTION 'off-origin page was queued';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_scope_invalid'; END;
    BEGIN
        PERFORM trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl_id,'pages',
            jsonb_build_array(jsonb_build_object('url','https://example.com/docsevil','depth',1))),repeat('a',32));
        RAISE EXCEPTION 'path prefix escaped its segment boundary';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_scope_invalid'; END;
    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl_id),repeat('a',32));
    ASSERT v_page->>'url'='https://example.com/docs' AND v_page->>'attempt'='1';
    ASSERT trellis.web_mutate('complete_page',v_scope||jsonb_build_object('crawl_id',v_crawl_id,'url',v_page->>'url',
        'source_id',v_source_id),repeat('a',32))->>'status'='completed';
    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl_id),repeat('a',32));
    ASSERT v_page->>'url'='https://example.com/docs/next';
    ASSERT trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl_id),repeat('a',32)) IS NULL,
        'one generation claimed the same page twice';

    v_result:=trellis.web_mutate('bind_citations',jsonb_build_object('run_id',v_run,'generation',0,'model_call_id',v_call,
        'citations',jsonb_build_array(jsonb_build_object('source_id',v_source_id,'start_line',1,'end_line',2))),repeat('a',32));
    ASSERT jsonb_array_length(v_result)=1 AND v_result->0->>'citation_index'='1';
    ASSERT v_result=trellis.web_mutate('read_citations',jsonb_build_object('session_id',v_scope->>'session_id','model_call_id',v_call));
    BEGIN
        PERFORM trellis.web_mutate('bind_citations',jsonb_build_object('run_id',v_run,'generation',0,'model_call_id',v_call,
            'citations',jsonb_build_array(jsonb_build_object('source_id',v_source_id,'start_line',1,'end_line',3))),repeat('a',32));
        RAISE EXCEPTION 'invalid citation lines bound';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='citation_invalid'; END;
    BEGIN
        PERFORM trellis.web_mutate('bind_citations',jsonb_build_object('run_id',v_run,'generation',0,'model_call_id',v_call,
            'citations','[]'::jsonb),repeat('a',32));
        RAISE EXCEPTION 'saved citation list was overwritten';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='citation_invalid'; END;

    PERFORM set_config('request.jwt.claim.sub','30000000-0000-0000-0000-000000000002',true);
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.web_sources),'RLS exposed another account';
    ASSERT trellis.web_mutate('read_source',jsonb_build_object('session_id',v_scope->>'session_id',
        'source_id',v_source_id,'start_line',1,'max_lines',20)) IS NULL;
    BEGIN
        PERFORM trellis.web_mutate('open_run',v_scope,repeat('a',32));
        RAISE EXCEPTION 'forged account opened web ledger';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM set_config('request.jwt.claim.sub','30000000-0000-0000-0000-000000000001',true);

    v_old_scope:=v_scope;
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',v_run),repeat('a',32));
    v_result:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',v_run,'expected_generation',0,
        'installation_id','30000000-0000-0000-0000-000000000099'),repeat('b',32));
    ASSERT (v_result->>'lease_acquired')::boolean AND v_result->'run'->>'generation'='1';
    PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
        'expected_dispatch_id',v_scope->>'dispatch_id','expected_attempt',1,'reconciliation','checkpoint_resume','proof',NULL),repeat('b',32));
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',v_run),repeat('b',32));
    v_dispatch:=gen_random_uuid();
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
        'dispatch_id',v_dispatch,'expected_attempt',1,'recovery_policy','checkpointed','input_snapshot',NULL),repeat('b',32));
    v_scope:=v_scope||jsonb_build_object('generation',1,'dispatch_id',v_dispatch,'dispatch_attempt',2);
    ASSERT trellis.web_mutate('open_run',v_scope,repeat('b',32))->'usage'=v_actual,'recovery reset allowance';
    BEGIN
        PERFORM trellis.web_mutate('settle',v_old_scope||jsonb_build_object('reservation_id',v_reservation,'usage',v_actual),repeat('b',32));
        RAISE EXCEPTION 'stale generation settled budget';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl_id),repeat('b',32));
    ASSERT v_page->>'url'='https://example.com/docs/next' AND v_page->>'attempt'='2' AND v_page->>'generation'='1';
    BEGIN
        PERFORM trellis.mutate('record_tool_result',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
            'content','Collected evidence.','status','completed','result_snapshot','{}'::jsonb),repeat('b',32));
        RAISE EXCEPTION 'checkpoint result accepted without snapshot';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='checkpointed result snapshot invalid'; END;
    v_result:=trellis.mutate('record_tool_result',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,'content','Collected evidence.',
        'status','completed','result_snapshot',jsonb_build_object('source','checkpointed_web','dispatch_id',v_dispatch,
            'generation',1,'content_sha256',encode(sha256(convert_to('Collected evidence.','UTF8')),'hex'))),repeat('b',32));
    ASSERT v_result->'tool_call'->'result_snapshot'->>'source'='checkpointed_web';
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,'next_status','interrupted',
        'event_type','run.interrupted','stop_reason','fixture_stop'),repeat('b',32));
    ASSERT (SELECT retry_allowed FROM trellis.runs WHERE id=v_run),'web work disabled safe continuation';
    v_result:=trellis.web_mutate('clear_sources',jsonb_build_object('session_id',v_scope->>'session_id'));
    ASSERT v_result='{"evidence_epoch":1,"cleared_count":1}'::jsonb;
    v_result:=trellis.web_mutate('read_source',jsonb_build_object('session_id',v_scope->>'session_id','source_id',v_source_id,'start_line',1,'max_lines',200));
    ASSERT v_result->'source'->>'status'='cleared' AND v_result->'lines'='[]'::jsonb;
    ASSERT v_result->'source'->>'requested_url' IS NULL AND v_result->'source'->>'title' IS NULL AND v_result->'source'->>'content_hash' IS NULL;
    ASSERT trellis.web_mutate('get_crawl',jsonb_build_object('session_id',v_scope->>'session_id','crawl_id',v_crawl_id))->>'seed_url' IS NULL;
    ASSERT (SELECT retained_bytes=0 FROM trellis.web_accounts WHERE user_id=auth.uid());
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl_id);
    BEGIN
        PERFORM trellis.web_mutate('record_source',v_scope||jsonb_build_object('reservation_id',v_reservation,'source',v_source),repeat('b',32));
        RAISE EXCEPTION 'late write recreated cleared evidence';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    ASSERT NOT has_function_privilege('anon','trellis.web_mutate(text,jsonb,text)','EXECUTE');
    ASSERT NOT has_table_privilege('authenticated','trellis.web_sources','INSERT');

    v_scope:=pg_temp.web_fixture('30000000-0000-0000-0000-000000000010','local-command','run_command');
    v_run:=(v_scope->>'run_id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,'next_status','interrupted',
        'event_type','run.interrupted','stop_reason','fixture_stop'),repeat('a',32));
    ASSERT (SELECT NOT retry_allowed AND uncertain_tool_call_id=(v_scope->>'tool_call_id')::uuid FROM trellis.runs WHERE id=v_run),
        'checkpoint support weakened local command uncertainty';
    ASSERT (SELECT status='outcome_unknown' FROM trellis.tool_calls WHERE id=(v_scope->>'tool_call_id')::uuid);

    v_scope:=pg_temp.web_fixture('30000000-0000-0000-0000-000000000011','checkpoint-limit');
    v_run:=(v_scope->>'run_id')::uuid; v_tool:=(v_scope->>'tool_call_id')::uuid; v_capability:=repeat('a',32);
    FOR v_attempt IN 1..3 LOOP
        PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',v_run),v_capability);
        v_next_capability:=repeat(chr(97+v_attempt),32);
        ASSERT (trellis.mutate('claim_recovery',jsonb_build_object('run_id',v_run,'expected_generation',v_attempt-1,
            'installation_id','30000000-0000-0000-0000-000000000099'),v_next_capability)->>'lease_acquired')::boolean;
        PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
            'expected_dispatch_id',v_scope->>'dispatch_id','expected_attempt',v_attempt,'reconciliation','checkpoint_resume','proof',NULL),v_next_capability);
        PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',v_run),v_next_capability);
        v_dispatch:=gen_random_uuid();
        PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
            'dispatch_id',v_dispatch,'expected_attempt',v_attempt,'recovery_policy','checkpointed','input_snapshot',NULL),v_next_capability);
        v_scope:=v_scope||jsonb_build_object('dispatch_id',v_dispatch); v_capability:=v_next_capability;
    END LOOP;
    ASSERT (SELECT dispatch_attempt=4 FROM trellis.tool_calls WHERE id=v_tool);
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',v_run),v_capability);
    v_result:=trellis.mutate('claim_recovery',jsonb_build_object('run_id',v_run,'expected_generation',3,
        'installation_id','30000000-0000-0000-0000-000000000099'),repeat('e',32));
    ASSERT NOT (v_result->>'lease_acquired')::boolean AND v_result->>'rejection_reason'='recovery_limit';
    ASSERT (v_result->'run'->>'retry_allowed')::boolean AND v_result->'run'->>'uncertain_tool_call_id' IS NULL;
    ASSERT (SELECT status='cancelled' AND dispatch_attempt=4 FROM trellis.tool_calls WHERE id=v_tool);

    v_scope:=pg_temp.web_fixture('30000000-0000-0000-0000-000000000011','cancel-old-attempt');
    v_run:=(v_scope->>'run_id')::uuid; v_tool:=(v_scope->>'tool_call_id')::uuid;
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',v_run),repeat('a',32));
    ASSERT (trellis.mutate('claim_recovery',jsonb_build_object('run_id',v_run,'expected_generation',0,
        'installation_id','30000000-0000-0000-0000-000000000099'),repeat('b',32))->>'lease_acquired')::boolean;
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',v_run),repeat('b',32));
    v_result:=jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,'content','The run stopped before another dispatch.',
        'status','cancelled','result_snapshot',jsonb_build_object('source','checkpointed_web','dispatch_id',v_scope->>'dispatch_id',
            'generation',0,'content_sha256',encode(sha256(convert_to('The run stopped before another dispatch.','UTF8')),'hex')));
    BEGIN
        PERFORM trellis.mutate('record_tool_result',v_result||'{"status":"completed"}',repeat('b',32));
        RAISE EXCEPTION 'old dispatch accepted a successful result after takeover';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='checkpointed result snapshot invalid'; END;
    BEGIN
        PERFORM trellis.mutate('record_tool_result',v_result,repeat('a',32));
        RAISE EXCEPTION 'old capability accepted terminal cleanup after takeover';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    v_again:=trellis.mutate('record_tool_result',v_result,repeat('b',32));
    ASSERT v_again->'tool_call'->>'status'='cancelled';
    ASSERT v_again=trellis.mutate('record_tool_result',v_result,repeat('b',32)),'terminal cleanup lost idempotence';
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,'next_status','interrupted',
        'event_type','run.interrupted','stop_reason','fixture_stop'),repeat('b',32));
    ASSERT (SELECT retry_allowed AND uncertain_tool_call_id IS NULL FROM trellis.runs WHERE id=v_run);

    v_scope:=pg_temp.web_fixture('30000000-0000-0000-0000-000000000011','failed-source');
    v_scope:=v_scope-'model_call_id'; v_run:=(v_scope->>'run_id')::uuid;
    PERFORM trellis.web_mutate('open_run',v_scope,repeat('a',32));
    v_reservation:=gen_random_uuid(); v_source_id:=gen_random_uuid();
    v_usage:='{"search_calls":0,"page_attempts":1,"network_requests":0,"response_bytes":0,"extracted_bytes":0}';
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_reservation,'operation_key','failed-page','usage',v_usage),repeat('a',32));
    v_source:=jsonb_build_object('id',v_source_id,'requested_url','https://example.com/unavailable','final_url',NULL,'canonical_url',NULL,
        'title',NULL,'publisher',NULL,'published_at',NULL,'fetched_at','2026-10-29T00:00:00Z','extractor_version','html-v1',
        'text','','status','unavailable','error_code','access_denied');
    BEGIN
        PERFORM trellis.web_mutate('record_source',v_scope||jsonb_build_object('reservation_id',v_reservation,
            'source',v_source-'requested_url'),repeat('a',32));
        RAISE EXCEPTION 'source without requested URL was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_scope_invalid'; END;
    v_result:=trellis.web_mutate('record_source',v_scope||jsonb_build_object('reservation_id',v_reservation,'source',v_source),repeat('a',32));
    ASSERT v_result->>'content_hash'=encode(sha256(convert_to('','UTF8')),'hex') AND v_result->>'line_count'='0';
    BEGIN
        PERFORM trellis.web_mutate('settle',v_scope||jsonb_build_object('reservation_id',v_reservation,
            'usage',v_usage||'{"page_attempts":0}'),repeat('a',32));
        RAISE EXCEPTION 'failed source refunded its page allowance';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_source_conflict'; END;
    PERFORM trellis.web_mutate('settle',v_scope||jsonb_build_object('reservation_id',v_reservation,'usage',v_usage),repeat('a',32));
    v_usage:='{"search_calls":0,"page_attempts":0,"network_requests":1,"response_bytes":0,"extracted_bytes":0}';
    v_actual:=v_usage||'{"network_requests":0}';
    FOR v_attempt IN 1..324 LOOP
        v_reservation:=gen_random_uuid();
        PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_reservation,
            'operation_key','empty-request:'||v_attempt::text,'usage',v_usage),repeat('a',32));
        PERFORM trellis.web_mutate('settle',v_scope||jsonb_build_object('reservation_id',v_reservation,'usage',v_actual),repeat('a',32));
    END LOOP;
    BEGIN
        PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',gen_random_uuid(),
            'operation_key','too-many-reservations','usage',v_usage),repeat('a',32));
        RAISE EXCEPTION 'zero-usage settlements minted unbounded reservation rows';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_budget_exhausted'; END;
    ASSERT (SELECT reservation_count=325 AND (usage->>'page_attempts')::integer=1 FROM trellis.web_runs WHERE run_id=v_run);
END;
$$;
RESET ROLE;
SET CONSTRAINTS ALL IMMEDIATE;
SET LOCAL ROLE anon;
DO $$
BEGIN
    ASSERT trellis.web_readiness()='{"schema_version":1,"ready":true}'::jsonb;
    ASSERT NOT has_table_privilege('anon','trellis.web_sources','SELECT');
    ASSERT NOT has_function_privilege('anon','trellis.web_mutate(text,jsonb,text)','EXECUTE');
END;
$$;
RESET ROLE;
ALTER TABLE trellis.tool_calls DROP CONSTRAINT tool_calls_recovery_policy_check;
SET LOCAL ROLE anon;
DO $$
BEGIN
    ASSERT trellis.web_readiness()='{"schema_version":1,"ready":false}'::jsonb,
        'web readiness ignored the missing checkpoint recovery constraint';
END;
$$;
ROLLBACK;
