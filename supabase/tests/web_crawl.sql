\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES ('33000000-0000-0000-0000-000000000001'),('33000000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES ('33000000-0000-0000-0000-000000000001'),('33000000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) VALUES
    ('33000000-0000-0000-0000-000000000010','33000000-0000-0000-0000-000000000001'),
    ('33000000-0000-0000-0000-000000000011','33000000-0000-0000-0000-000000000001'),
    ('33000000-0000-0000-0000-000000000012','33000000-0000-0000-0000-000000000001'),
    ('33000000-0000-0000-0000-000000000013','33000000-0000-0000-0000-000000000001');
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
SELECT set_config('request.jwt.claim.sub','33000000-0000-0000-0000-000000000001',true);
DO $$
DECLARE v_scope jsonb; v_old_scope jsonb; v_payload jsonb; v_seed_payload jsonb; v_page jsonb; v_result jsonb;
    v_crawl uuid:='33000000-0000-0000-0000-000000000020'; v_res uuid; v_old_res uuid;
    v_usage jsonb:='{"search_calls":0,"page_attempts":1,"network_requests":7,"response_bytes":18874368,"extracted_bytes":262144}';
    v_actual jsonb:='{"search_calls":0,"page_attempts":1,"network_requests":2,"response_bytes":100,"extracted_bytes":6}';
    v_source jsonb; v_links jsonb; v_run uuid; v_tool uuid; v_dispatch uuid; v_bad text;
BEGIN
    ASSERT trellis.web_readiness()='{"schema_version":3,"ready":true}'::jsonb;
    ASSERT NOT has_table_privilege('authenticated','trellis_private.web_crawl_origins','SELECT');
    ASSERT NOT has_table_privilege('authenticated','trellis_private.web_crawl_origin_attempts','SELECT');
    v_scope:=pg_temp.web_fixture('33000000-0000-0000-0000-000000000010','crawl-atomic')-'model_call_id';
    v_run:=(v_scope->>'run_id')::uuid; v_tool:=(v_scope->>'tool_call_id')::uuid;
    PERFORM trellis.web_mutate('open_run',v_scope,repeat('a',32));
    PERFORM trellis.web_mutate('create_crawl',v_scope||jsonb_build_object('crawl_id',v_crawl,
        'seed_url','https://example.com/docs','origin','https://example.com','path_prefix','/docs','max_pages',3,'max_depth',2),repeat('a',32));
    FOREACH v_bad IN ARRAY ARRAY['https://example.com/docsevil','https://example.com/docs/../outside',
        'https://example.com/docs/%252e%252e/outside','https://example.com/docs/%25252f..%25252foutside',
        'https://example.com/docs/%255coutside','https://example.com/docs/%2500','https://example.com/docs#fragment',
        'https://other.example/docs','https://example.com/docs/%zz'] LOOP
        BEGIN
            PERFORM trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl,
                'pages',jsonb_build_array(jsonb_build_object('url',v_bad,'depth',1))),repeat('a',32));
            RAISE EXCEPTION 'unsafe scope accepted: %',v_bad;
        EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_scope_invalid'; END;
    END LOOP;
    PERFORM trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl,
        'pages',jsonb_build_array(jsonb_build_object('url','https://example.com/docs','depth',0))),repeat('a',32));
    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('a',32));
    ASSERT v_page->>'url'='https://example.com/docs' AND v_page->>'attempt'='1';
    v_res:=gen_random_uuid();
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_res,'operation_key','crawl:seed:1','usage',v_usage),repeat('a',32));
    v_payload:=v_scope||jsonb_build_object('crawl_id',v_crawl,'url',v_page->>'url','page_attempt',1);
    ASSERT trellis.web_mutate('reserve_crawl_origin',v_payload,repeat('a',32))='{"wait_seconds":0}'::jsonb;
    ASSERT trellis.web_mutate('reserve_crawl_origin',v_payload,repeat('a',32))='{"wait_seconds":0}'::jsonb;
    v_source:=jsonb_build_object('id',gen_random_uuid(),'requested_url',v_page->>'url','final_url',v_page->>'url',
        'canonical_url','https://outside.example/no-authority','title','Crawl fixture','publisher',NULL,'published_at',NULL,
        'fetched_at','2026-10-30T00:00:00Z','extractor_version','fixture-v1','text','Facts.','status','success','error_code',NULL);
    v_links:='["https://example.com/docs/b?x=1","https://example.com/docs/a","https://example.com/docs/a"]';
    v_payload:=v_payload||jsonb_build_object('reservation_id',v_res,'source',v_source,'usage',v_actual,'discovered_links',v_links);
    BEGIN
        PERFORM trellis.web_mutate('finalize_crawl_page',v_payload||jsonb_build_object('usage',v_actual||'{"network_requests":8}'),repeat('a',32));
        RAISE EXCEPTION 'oversized settlement finalized';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_budget_exhausted'; END;
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.web_sources WHERE run_id=v_run),'failed settlement retained a source';
    ASSERT (SELECT status='fetching' AND discovered_links='[]'::jsonb FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl);
    v_result:=trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32));
    ASSERT v_result->>'status'='completed' AND v_result->'discovered_links'=v_links;
    ASSERT v_result=trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32));
    v_seed_payload:=v_payload;
    ASSERT (SELECT count(*)=3 FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl),'links were not atomically queued';
    ASSERT (SELECT status='settled' AND actual=v_actual FROM trellis.web_reservations WHERE id=v_res);
    BEGIN
        PERFORM trellis.web_mutate('finalize_crawl_page',v_payload||'{"discovered_links":[]}',repeat('a',32));
        RAISE EXCEPTION 'replay changed its link snapshot';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_source_conflict'; END;
    PERFORM set_config('request.jwt.claim.sub','33000000-0000-0000-0000-000000000002',true);
    BEGIN
        PERFORM trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32));
        RAISE EXCEPTION 'another owner finalized a crawl';
    EXCEPTION WHEN insufficient_privilege THEN ASSERT SQLERRM='web_scope_invalid'; END;
    PERFORM set_config('request.jwt.claim.sub','33000000-0000-0000-0000-000000000001',true);

    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('a',32));
    ASSERT v_page->>'url'='https://example.com/docs/a';
    v_res:=gen_random_uuid();
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_res,'operation_key','crawl:a:1','usage',v_usage),repeat('a',32));
    SELECT jsonb_agg(jsonb_build_object('url','https://example.com/docs/queued/'||i,'depth',2)) INTO v_links FROM generate_series(1,197) i;
    PERFORM trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl,'pages',v_links),repeat('a',32));
    SELECT jsonb_agg('https://example.com/docs/queued/'||i) INTO v_links FROM generate_series(1,32) i;
    v_source:=v_source||jsonb_build_object('id',gen_random_uuid(),'requested_url',v_page->>'url','final_url',v_page->>'url');
    v_payload:=v_scope||jsonb_build_object('crawl_id',v_crawl,'url',v_page->>'url','page_attempt',1,
        'reservation_id',v_res,'source',v_source,'usage',v_actual,'discovered_links',v_links);
    v_result:=trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32));
    ASSERT v_result->'discovered_links'=v_links;
    ASSERT (SELECT count(*)=200 FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl),'frontier cap lost its bound';
    ASSERT (SELECT count(*)=2 FROM trellis.web_sources WHERE run_id=v_run),'full frontier lost completed evidence';
    ASSERT (SELECT status='running' AND stop_reason IS NULL FROM trellis.web_crawls WHERE id=v_crawl),
        'duplicate links stopped a full frontier';
    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('a',32));
    ASSERT v_page->>'url'='https://example.com/docs/b?x=1';
    v_old_res:=gen_random_uuid();
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_old_res,
        'operation_key','crawl:b:1','usage',v_usage),repeat('a',32));
    BEGIN
        PERFORM trellis.web_mutate('clear_sources',jsonb_build_object('session_id',v_scope->>'session_id'));
        RAISE EXCEPTION 'active crawl evidence cleared';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_sources_busy'; END;
    v_old_scope:=v_scope;
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',v_run),repeat('a',32));
    ASSERT (trellis.mutate('claim_recovery',jsonb_build_object('run_id',v_run,'expected_generation',0,
        'installation_id','30000000-0000-0000-0000-000000000099'),repeat('b',32))->>'lease_acquired')::boolean;
    PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
        'expected_dispatch_id',v_scope->>'dispatch_id','expected_attempt',1,'reconciliation','checkpoint_resume','proof',NULL),repeat('b',32));
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',v_run),repeat('b',32));
    v_dispatch:=gen_random_uuid();
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',v_run,'tool_call_id',v_tool,
        'dispatch_id',v_dispatch,'expected_attempt',1,'recovery_policy','checkpointed','input_snapshot',NULL),repeat('b',32));
    v_scope:=v_scope||jsonb_build_object('generation',1,'dispatch_id',v_dispatch,'dispatch_attempt',2);
    ASSERT (trellis.web_mutate('open_run',v_scope,repeat('b',32))->'usage'->>'response_bytes')::integer=18874568;
    BEGIN
        PERFORM trellis.web_mutate('finalize_crawl_page',v_payload,repeat('b',32));
        RAISE EXCEPTION 'old dispatch finalized after takeover';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('b',32));
    ASSERT v_page->>'url'='https://example.com/docs/b?x=1' AND v_page->>'attempt'='2';
    ASSERT (SELECT count(*)=2 FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl AND status='completed');
    v_res:=gen_random_uuid();
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_res,'operation_key','crawl:b:2','usage',v_usage),repeat('b',32));
    v_source:=v_source||jsonb_build_object('id',gen_random_uuid(),'requested_url',v_page->>'url','final_url',v_page->>'url');
    v_payload:=v_scope||jsonb_build_object('crawl_id',v_crawl,'url',v_page->>'url','page_attempt',2,
        'reservation_id',v_res,'source',v_source,'usage',v_actual,'discovered_links','[]'::jsonb);
    PERFORM trellis.web_mutate('finalize_crawl_page',v_payload,repeat('b',32));
    ASSERT (SELECT status='reserved' AND actual IS NULL FROM trellis.web_reservations WHERE id=v_old_res);
    ASSERT (SELECT (usage->>'page_attempts')::integer=4 AND (usage->>'response_bytes')::integer=18874668
        FROM trellis.web_runs WHERE run_id=v_run),'takeover reset or refunded an unfinished attempt';
    ASSERT trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('b',32)) IS NULL;
    ASSERT (SELECT count(*)=3 FROM trellis.web_sources WHERE run_id=v_run);
    ASSERT (SELECT discovered_links=v_seed_payload->'discovered_links' FROM trellis.web_crawl_pages
        WHERE crawl_id=v_crawl AND url='https://example.com/docs');
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',v_run,'next_status','interrupted',
        'event_type','run.interrupted','stop_reason','fixture_stop'),repeat('b',32));
    ASSERT trellis.web_mutate('clear_sources',jsonb_build_object('session_id',v_scope->>'session_id'))=
        '{"evidence_epoch":1,"cleared_count":3}'::jsonb;
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl);
    ASSERT (SELECT seed_url IS NULL AND origin IS NULL AND path_prefix IS NULL FROM trellis.web_crawls WHERE id=v_crawl);
    BEGIN
        PERFORM trellis.web_mutate('finalize_crawl_page',v_payload,repeat('b',32));
        RAISE EXCEPTION 'late completion restored cleared links';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    PERFORM set_config('fixture.cleared_crawl',v_crawl::text,true);
END;
$$;
RESET ROLE;
DO $$
BEGIN
    ASSERT NOT EXISTS(SELECT 1 FROM trellis_private.web_crawl_origin_attempts
        WHERE crawl_id=current_setting('fixture.cleared_crawl')::uuid),'clear left a URL-bearing pacing record';
END;
$$;
SET LOCAL ROLE authenticated;
DO $$
DECLARE v_scope jsonb; v_crawl uuid:=gen_random_uuid(); v_res uuid:=gen_random_uuid(); v_source jsonb;
BEGIN
    v_scope:=pg_temp.web_fixture('33000000-0000-0000-0000-000000000011','crawl-quota')-'model_call_id';
    PERFORM trellis.web_mutate('open_run',v_scope,repeat('a',32));
    PERFORM trellis.web_mutate('create_crawl',v_scope||jsonb_build_object('crawl_id',v_crawl,'seed_url','https://quota.example/docs',
        'origin','https://quota.example','path_prefix','/docs','max_pages',1,'max_depth',0),repeat('a',32));
    PERFORM trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl,
        'pages','[{"url":"https://quota.example/docs","depth":0}]'::jsonb),repeat('a',32));
    PERFORM trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('a',32));
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_res,'operation_key','quota-page',
        'usage','{"search_calls":0,"page_attempts":1,"network_requests":7,"response_bytes":18874368,"extracted_bytes":262144}'::jsonb),repeat('a',32));
    v_source:=jsonb_build_object('id',gen_random_uuid(),'requested_url','https://quota.example/docs','final_url','https://quota.example/docs',
        'canonical_url',NULL,'title',NULL,'publisher',NULL,'published_at',NULL,'fetched_at','2026-10-30T00:00:00Z',
        'extractor_version','fixture-v1','text','Facts.','status','success','error_code',NULL);
    PERFORM set_config('fixture.quota_payload',(v_scope||jsonb_build_object('crawl_id',v_crawl,'url','https://quota.example/docs',
        'page_attempt',1,'reservation_id',v_res,'source',v_source,'discovered_links','[]'::jsonb,
        'usage','{"search_calls":0,"page_attempts":1,"network_requests":2,"response_bytes":100,"extracted_bytes":6}'::jsonb))::text,true);
END;
$$;
RESET ROLE;
UPDATE trellis.web_accounts SET retained_bytes=104857595 WHERE user_id='33000000-0000-0000-0000-000000000001';
SET LOCAL ROLE authenticated;
DO $$
DECLARE v_payload jsonb:=current_setting('fixture.quota_payload')::jsonb; v_result jsonb;
BEGIN
    BEGIN
        PERFORM trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32));
        RAISE EXCEPTION 'full owner quota saved source text';
    EXCEPTION WHEN invalid_parameter_value THEN ASSERT SQLERRM='web_storage_limit'; END;
    ASSERT (SELECT status='fetching' AND source_id IS NULL FROM trellis.web_crawl_pages WHERE crawl_id=(v_payload->>'crawl_id')::uuid);
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.web_sources WHERE id=(v_payload->'source'->>'id')::uuid);
    ASSERT (SELECT status='reserved' FROM trellis.web_reservations WHERE id=(v_payload->>'reservation_id')::uuid);
    v_payload:=jsonb_set(v_payload,'{source}',v_payload->'source'||'{"text":"","status":"budget_exhausted","error_code":"web_storage_limit"}'::jsonb);
    v_payload:=jsonb_set(v_payload,'{usage}',v_payload->'usage'||'{"extracted_bytes":0}'::jsonb);
    v_result:=trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32));
    ASSERT v_result->>'status'='completed';
    ASSERT (SELECT status='budget_exhausted' AND byte_count=0 FROM trellis.web_sources WHERE id=(v_payload->'source'->>'id')::uuid);
    ASSERT (SELECT (usage->>'response_bytes')::integer=100 AND (usage->>'extracted_bytes')::integer=0
        FROM trellis.web_runs WHERE run_id=(v_payload->>'run_id')::uuid);
END;
$$;
RESET ROLE;
UPDATE trellis.web_accounts SET retained_bytes=0 WHERE user_id='33000000-0000-0000-0000-000000000001';
SET LOCAL ROLE authenticated;
DO $$
DECLARE v_scope jsonb; v_crawl uuid:=gen_random_uuid(); v_page jsonb; v_res uuid:=gen_random_uuid();
BEGIN
    v_scope:=pg_temp.web_fixture('33000000-0000-0000-0000-000000000012','crawl-same-generation')-'model_call_id';
    PERFORM trellis.web_mutate('open_run',v_scope,repeat('a',32));
    PERFORM trellis.web_mutate('create_crawl',v_scope||jsonb_build_object('crawl_id',v_crawl,
        'seed_url','https://encoded.example/%64ocs','origin','https://encoded.example','path_prefix','/docs','max_pages',1,'max_depth',1),repeat('a',32));
    PERFORM trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl,
        'pages','[{"url":"https://encoded.example/%64ocs","depth":0}]'::jsonb),repeat('a',32));
    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('a',32));
    ASSERT v_page->>'attempt'='1' AND v_page->>'generation'='0';
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_res,'operation_key','unfinished:1',
        'usage','{"search_calls":0,"page_attempts":1,"network_requests":7,"response_bytes":18874368,"extracted_bytes":262144}'::jsonb),repeat('a',32));
    PERFORM set_config('fixture.retry_scope',v_scope::text,true);
    PERFORM set_config('fixture.retry_crawl',v_crawl::text,true);
    PERFORM set_config('fixture.retry_reservation',v_res::text,true);
END;
$$;
RESET ROLE;
DO $$
DECLARE v_scope jsonb:=current_setting('fixture.retry_scope')::jsonb; v_dispatch uuid:=gen_random_uuid();
BEGIN
    UPDATE trellis.tool_calls SET dispatch_id=v_dispatch,dispatch_attempt=2
        WHERE id=(v_scope->>'tool_call_id')::uuid;
    PERFORM set_config('fixture.retry_new_scope',(v_scope||jsonb_build_object('dispatch_id',v_dispatch,'dispatch_attempt',2))::text,true);
END;
$$;
SET LOCAL ROLE authenticated;
DO $$
DECLARE v_scope jsonb:=current_setting('fixture.retry_new_scope')::jsonb; v_crawl uuid:=current_setting('fixture.retry_crawl')::uuid;
    v_page jsonb; v_res uuid:=gen_random_uuid(); v_payload jsonb;
BEGIN
    BEGIN
        PERFORM trellis.web_mutate('claim_page',current_setting('fixture.retry_scope')::jsonb||jsonb_build_object('crawl_id',v_crawl),repeat('a',32));
        RAISE EXCEPTION 'prior dispatch claimed a same-generation retry';
    EXCEPTION WHEN insufficient_privilege THEN ASSERT SQLERRM='web_scope_invalid'; END;
    v_page:=trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('a',32));
    ASSERT v_page->>'attempt'='2' AND v_page->>'generation'='0';
    ASSERT v_page->>'claimed_dispatch_id'=v_scope->>'dispatch_id';
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_res,'operation_key','retry:2',
        'usage','{"search_calls":0,"page_attempts":1,"network_requests":7,"response_bytes":18874368,"extracted_bytes":262144}'::jsonb),repeat('a',32));
    v_payload:=v_scope||jsonb_build_object('crawl_id',v_crawl,'url',v_page->>'url','page_attempt',2,'reservation_id',v_res,
        'source',jsonb_build_object('id',gen_random_uuid(),'requested_url',v_page->>'url','final_url',v_page->>'url','canonical_url',NULL,
            'title',NULL,'publisher',NULL,'published_at',NULL,'fetched_at','2026-10-30T00:00:00Z','extractor_version','fixture-v1',
            'text','Facts.','status','success','error_code',NULL),
        'usage','{"search_calls":0,"page_attempts":1,"network_requests":1,"response_bytes":100,"extracted_bytes":6}'::jsonb,
        'discovered_links','["https://encoded.example/%64ocs/next"]'::jsonb);
    PERFORM trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32));
    ASSERT (SELECT status='reserved' FROM trellis.web_reservations WHERE id=current_setting('fixture.retry_reservation')::uuid);
    ASSERT (SELECT (usage->>'page_attempts')::integer=2 AND (usage->>'response_bytes')::integer=18874468
        FROM trellis.web_runs WHERE run_id=(v_scope->>'run_id')::uuid);
    ASSERT trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('a',32)) IS NULL;
END;
$$;
RESET ROLE;
SET LOCAL ROLE authenticated;
DO $$
DECLARE v_scope jsonb; v_crawl uuid:=gen_random_uuid(); v_res uuid:=gen_random_uuid(); v_links jsonb;
    v_payload jsonb; v_result jsonb;
BEGIN
    v_scope:=pg_temp.web_fixture('33000000-0000-0000-0000-000000000013','crawl-frontier-limit')-'model_call_id';
    PERFORM trellis.web_mutate('open_run',v_scope,repeat('a',32));
    PERFORM trellis.web_mutate('create_crawl',v_scope||jsonb_build_object('crawl_id',v_crawl,
        'seed_url','https://frontier.example/docs','origin','https://frontier.example','path_prefix','/docs','max_pages',20,'max_depth',1),repeat('a',32));
    SELECT jsonb_agg(jsonb_build_object('url','https://frontier.example/docs/queued/'||i,'depth',1)) INTO v_links FROM generate_series(1,199) i;
    v_links:=v_links||'[{"url":"https://frontier.example/docs","depth":0}]'::jsonb;
    PERFORM trellis.web_mutate('enqueue_pages',v_scope||jsonb_build_object('crawl_id',v_crawl,'pages',v_links),repeat('a',32));
    ASSERT trellis.web_mutate('claim_page',v_scope||jsonb_build_object('crawl_id',v_crawl),repeat('a',32))->>'url'='https://frontier.example/docs';
    PERFORM trellis.web_mutate('reserve',v_scope||jsonb_build_object('reservation_id',v_res,'operation_key','frontier:1',
        'usage','{"search_calls":0,"page_attempts":1,"network_requests":7,"response_bytes":18874368,"extracted_bytes":262144}'::jsonb),repeat('a',32));
    SELECT jsonb_agg('https://frontier.example/docs/new/'||i) INTO v_links FROM generate_series(1,32) i;
    v_payload:=v_scope||jsonb_build_object('crawl_id',v_crawl,'url','https://frontier.example/docs','page_attempt',1,
        'reservation_id',v_res,'discovered_links',v_links,
        'source',jsonb_build_object('id',gen_random_uuid(),'requested_url','https://frontier.example/docs','final_url','https://frontier.example/docs',
            'canonical_url',NULL,'title',NULL,'publisher',NULL,'published_at',NULL,'fetched_at','2026-10-30T00:00:00Z',
            'extractor_version','fixture-v1','text','Facts.','status','success','error_code',NULL),
        'usage','{"search_calls":0,"page_attempts":1,"network_requests":2,"response_bytes":100,"extracted_bytes":6}'::jsonb);
    v_result:=trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32));
    ASSERT v_result->>'status'='completed' AND v_result->'discovered_links'=v_links;
    ASSERT (SELECT status='stopped' AND stop_reason='frontier_limit' FROM trellis.web_crawls WHERE id=v_crawl),
        'new accepted links were silently dropped at the frontier limit';
    ASSERT (SELECT count(*)=200 FROM trellis.web_crawl_pages WHERE crawl_id=v_crawl);
    ASSERT (SELECT count(*)=1 FROM trellis.web_sources WHERE run_id=(v_scope->>'run_id')::uuid);
    ASSERT (SELECT status='settled' AND actual=v_payload->'usage' FROM trellis.web_reservations WHERE id=v_res);
    ASSERT (SELECT usage=v_payload->'usage' FROM trellis.web_runs WHERE run_id=(v_scope->>'run_id')::uuid);
    ASSERT v_result=trellis.web_mutate('finalize_crawl_page',v_payload,repeat('a',32)),
        'frontier stop broke atomic completion replay';
    ASSERT (SELECT status='stopped' AND stop_reason='frontier_limit' FROM trellis.web_crawls WHERE id=v_crawl);
END;
$$;
RESET ROLE;
DELETE FROM auth.users WHERE id='33000000-0000-0000-0000-000000000001';
SET CONSTRAINTS ALL IMMEDIATE;
DO $$
BEGIN
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.web_crawl_pages WHERE user_id='33000000-0000-0000-0000-000000000001');
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.web_sources WHERE user_id='33000000-0000-0000-0000-000000000001');
    ASSERT NOT EXISTS(SELECT 1 FROM trellis_private.web_crawl_origin_attempts);
END;
$$;
ROLLBACK;
