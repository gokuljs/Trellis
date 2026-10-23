\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES
    ('23500000-0000-0000-0000-000000000001'),('23500000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES
    ('23500000-0000-0000-0000-000000000001'),('23500000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23500000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
    '23500000-0000-0000-0000-000000000001'::uuid FROM generate_series(50,59) n;
CREATE TEMP TABLE reconciliation_fixture(n integer PRIMARY KEY,run_id uuid,tool_id uuid,dispatch_id uuid,
    input_snapshot jsonb,proof jsonb,policy text);
GRANT SELECT,INSERT ON reconciliation_fixture TO authenticated;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','23500000-0000-0000-0000-000000000001',true);
DO $$
DECLARE p jsonb; r jsonb; c jsonb; a jsonb; tool_name text; policy text; preview jsonb; input jsonb; dispatch uuid;
BEGIN
    FOR n IN 50..59 LOOP
        tool_name:=CASE WHEN n=53 THEN 'run_command' WHEN n IN (54,57) THEN 'read_file' ELSE 'apply_patch' END;
        policy:=CASE WHEN n=53 THEN 'command' WHEN n IN (54,57) THEN 'read_only' ELSE 'patch' END;
        p:=jsonb_build_object('session_id','23500000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','tools','client_request_id','tools','content','Work','request_fingerprint',repeat('a',64),
            'budget_preset','conservative','model_id',(SELECT id FROM trellis.models WHERE enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions',jsonb_build_object(tool_name,'v1'),
                'request_identity','tools','installation_id','23500000-0000-0000-0000-000000000099','workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
            'model_id',p->>'model_id','request_snapshot','{"purpose":"generation"}'::jsonb),repeat('a',32));
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','completed'),repeat('a',32));
        a:=trellis.mutate('record_assistant_message',jsonb_build_object('run_id',r#>>'{run,id}','model_call_id',c->>'id',
            'message',jsonb_build_object('role','assistant','content','',
                'tool_calls',jsonb_build_array(jsonb_build_object('id','tool-1','name',tool_name,
                    'arguments',jsonb_build_object('path','a.py'))))),repeat('a',32));
        preview:=NULL;
        IF policy<>'read_only' THEN
            preview:=jsonb_build_object('target_sha256',CASE WHEN n=51 THEN NULL ELSE repeat('a',64) END,'desired_sha256',repeat('b',64));
            PERFORM trellis.mutate('request_tool_approval',jsonb_build_object('run_id',r#>>'{run,id}',
                'tool_call_id',a#>>'{tool_calls,0,id}','preview',preview),repeat('a',32));
            PERFORM trellis.mutate('record_tool_approval_decision',jsonb_build_object('run_id',r#>>'{run,id}',
                'tool_call_id',a#>>'{tool_calls,0,id}','decision','approved'),repeat('a',32));
            PERFORM trellis.mutate('resume_approved_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        END IF;
        input:=jsonb_build_object('arguments',jsonb_build_object('path','a.py'),'approval_preview',preview);
        dispatch:=('23500000-0000-0000-0000-'||lpad((n*10+1)::text,12,'0'))::uuid;
        PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',r#>>'{run,id}',
            'tool_call_id',a#>>'{tool_calls,0,id}','dispatch_id',dispatch,'expected_attempt',0,
            'recovery_policy',policy,'input_snapshot',input),repeat('a',32));
        INSERT INTO reconciliation_fixture VALUES(n,(r#>>'{run,id}')::uuid,(a#>>'{tool_calls,0,id}')::uuid,dispatch,input,
            jsonb_build_object('before_sha256',preview->'target_sha256','after_sha256',preview->'desired_sha256',
                'observed_sha256',preview->'target_sha256'),policy);
        PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',r#>>'{run,id}','expected_generation',0,
            'installation_id','23500000-0000-0000-0000-000000000099'),repeat('b',32));
        PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('b',32));
    END LOOP;
END $$;
DO $$
DECLARE f record; p jsonb; changed jsonb; authorized jsonb; result jsonb; metadata jsonb; count_before integer;
BEGIN
    SELECT * INTO f FROM reconciliation_fixture WHERE n=50;
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'expected_dispatch_id',f.dispatch_id,
        'expected_attempt',1,'reconciliation','unchanged_original','proof',f.proof);
    FOREACH changed IN ARRAY ARRAY[
        jsonb_set(p,'{proof,before_sha256}',to_jsonb(repeat('c',64))),
        jsonb_set(p,'{proof,after_sha256}',to_jsonb(repeat('c',64))),
        jsonb_set(p,'{proof,observed_sha256}',to_jsonb(repeat('b',64))),
        jsonb_set(p,'{expected_dispatch_id}','"23500000-0000-0000-0000-000000000999"'),
        jsonb_set(p,'{expected_attempt}','0')
    ] LOOP
        BEGIN
            PERFORM trellis.mutate('authorize_tool_retry',changed,repeat('b',32));
            RAISE EXCEPTION 'changed patch proof or dispatch source was authorized';
        EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    END LOOP;
    BEGIN
        PERFORM trellis.mutate('authorize_tool_retry',p,repeat('a',32));
        RAISE EXCEPTION 'a stale owner authorized a patch retry';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    count_before:=(SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id);
    authorized:=trellis.mutate('authorize_tool_retry',p,repeat('b',32));
    ASSERT authorized->>'status'='pending' AND authorized->>'dispatch_attempt'='1';
    ASSERT authorized->>'dispatch_id'=f.dispatch_id::text AND authorized->>'dispatch_generation'='0';
    BEGIN
        PERFORM trellis.mutate('record_tool_result',jsonb_build_object('run_id',f.run_id,
            'tool_call_id',f.tool_id,'content','Cancelled before retry','status','cancelled'),repeat('b',32));
        RAISE EXCEPTION 'a synthetic result erased a prior dispatched patch';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    ASSERT trellis.mutate('authorize_tool_retry',p,repeat('b',32))=authorized;
    ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=f.run_id)=count_before+1;
    BEGIN
        PERFORM trellis.mutate('authorize_tool_retry',jsonb_set(p,'{proof,observed_sha256}',to_jsonb(repeat('c',64))),repeat('b',32));
        RAISE EXCEPTION 'saved retry proof was changed';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'dispatch_id','23500000-0000-0000-0000-000000000502','expected_attempt',1,
        'recovery_policy','patch','input_snapshot',f.input_snapshot);
    result:=trellis.mutate('begin_tool_execution',p,repeat('b',32));
    ASSERT result#>>'{tool_call,status}'='running' AND result#>>'{tool_call,dispatch_attempt}'='2';
    ASSERT result#>>'{tool_call,dispatch_generation}'='1';
    ASSERT trellis.mutate('begin_tool_execution',p,repeat('b',32))=result;
    metadata:='{"source":"executor","workspace_observations":[{"path":"a.py","sha256":"saved"}]}';
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'content','Patch applied.',
        'status','completed','result_source','executor','result_snapshot',metadata);
    result:=trellis.mutate('record_tool_result',p,repeat('b',32));
    ASSERT result#>'{tool_call,result_snapshot}'=metadata;
    ASSERT trellis.mutate('record_tool_result',p,repeat('b',32))=result;
    ASSERT (SELECT result_snapshot=metadata FROM trellis.tool_calls WHERE id=f.tool_id);
    BEGIN
        PERFORM trellis.mutate('record_tool_result',jsonb_set(p,'{result_snapshot,source}','"changed"'),repeat('b',32));
        RAISE EXCEPTION 'result observation metadata changed after commit';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;
DO $$
DECLARE f record; p jsonb; authorized jsonb; begun jsonb; current_dispatch uuid; attempt integer;
BEGIN
    SELECT * INTO f FROM reconciliation_fixture WHERE n=51;
    current_dispatch:=f.dispatch_id;
    FOR generation IN 1..3 LOOP
        p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'expected_dispatch_id',current_dispatch,
            'expected_attempt',generation,'reconciliation','unchanged_original','proof',f.proof);
        authorized:=trellis.mutate('authorize_tool_retry',p,repeat(chr(97+generation),32));
        ASSERT authorized->>'status'='pending','an absent original file is represented by a null before hash';
        current_dispatch:=('23500000-0000-0000-0000-'||lpad((510+generation+1)::text,12,'0'))::uuid;
        begun:=trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'dispatch_id',current_dispatch,'expected_attempt',generation,'recovery_policy','patch','input_snapshot',f.input_snapshot),
            repeat(chr(97+generation),32));
        ASSERT (begun#>>'{tool_call,dispatch_attempt}')::integer=generation+1;
        IF generation<3 THEN
            PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat(chr(97+generation),32));
            PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,'expected_generation',generation,
                'installation_id','23500000-0000-0000-0000-000000000099'),repeat(chr(98+generation),32));
            PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',f.run_id),repeat(chr(98+generation),32));
        END IF;
    END LOOP;
    ASSERT (SELECT dispatch_attempt=4 FROM trellis.tool_calls WHERE id=f.tool_id);
    BEGIN
        PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'expected_dispatch_id',current_dispatch,'expected_attempt',4,'reconciliation','unchanged_original','proof',f.proof),repeat('d',32));
        RAISE EXCEPTION 'patch retry exceeded the original recovery budget';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;
DO $$
DECLARE f record; p jsonb; result jsonb; proof jsonb; metadata jsonb;
BEGIN
    SELECT * INTO f FROM reconciliation_fixture WHERE n=52;
    proof:=jsonb_set(f.proof,'{observed_sha256}',f.proof->'after_sha256');
    metadata:=jsonb_build_object('source','reconciled_desired_state','reconciliation_proof',proof,
        'dispatch_id',f.dispatch_id,'attempt',1,'workspace_observations','[]'::jsonb);
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'content','The approved desired file state is present; the original acknowledgment was lost.',
        'status','completed','result_source','reconciled_desired_state','result_snapshot',metadata);
    BEGIN
        PERFORM trellis.mutate('record_tool_result',jsonb_set(p,'{result_snapshot,reconciliation_proof,observed_sha256}',f.proof->'before_sha256'),repeat('b',32));
        RAISE EXCEPTION 'an unchanged original was claimed as applied';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    result:=trellis.mutate('record_tool_result',p,repeat('b',32));
    ASSERT result#>>'{tool_call,status}'='completed' AND result#>>'{tool_call,dispatch_attempt}'='1',
        'reconciliation records the existing desired state without another dispatch';
    ASSERT result#>'{tool_call,result_snapshot}'=metadata;
    ASSERT result#>>'{tool_call,result_source}'='reconciled_desired_state';
    ASSERT trellis.mutate('record_tool_result',p,repeat('b',32))=result;
    ASSERT (SELECT count(*) FROM trellis.run_messages WHERE tool_call_id=f.tool_id)=1;
    PERFORM set_config('trellis.test_reconciled_result',p::text,true);
    SELECT * INTO f FROM reconciliation_fixture WHERE n=53;
    BEGIN
        PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'expected_dispatch_id',f.dispatch_id,'expected_attempt',1,'reconciliation','unchanged_original','proof',f.proof),repeat('b',32));
        RAISE EXCEPTION 'a command was authorized as a patch retry';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'expected_dispatch_id',f.dispatch_id,'expected_attempt',1,'reconciliation','read_retry','proof',NULL),repeat('b',32));
        RAISE EXCEPTION 'a command was authorized as a read retry';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    p:=p||jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id);
    BEGIN
        PERFORM trellis.mutate('record_tool_result',p,repeat('b',32));
        RAISE EXCEPTION 'a command was falsely reconciled from patch hashes';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;
DO $$
DECLARE f record; p jsonb; result jsonb;
BEGIN
    SELECT * INTO f FROM reconciliation_fixture WHERE n=54;
    PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'expected_dispatch_id',f.dispatch_id,'expected_attempt',1,'reconciliation','read_retry','proof',NULL),repeat('b',32));
    PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'dispatch_id','23500000-0000-0000-0000-000000000542','expected_attempt',1,'recovery_policy','read_only','input_snapshot',f.input_snapshot),repeat('b',32));
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('b',32));
    PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,'expected_generation',1,
        'installation_id','23500000-0000-0000-0000-000000000099'),repeat('c',32));
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',f.run_id),repeat('c',32));
    BEGIN
        PERFORM trellis.mutate('authorize_tool_retry',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'expected_dispatch_id','23500000-0000-0000-0000-000000000542','expected_attempt',2,
            'reconciliation','read_retry','proof',NULL),repeat('c',32));
        RAISE EXCEPTION 'read retry exceeded one replacement attempt';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    SELECT * INTO f FROM reconciliation_fixture WHERE n=55;
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'expected_dispatch_id',f.dispatch_id,
        'expected_attempt',1,'reconciliation','unchanged_original','proof',f.proof);
    PERFORM trellis.mutate('authorize_tool_retry',p,repeat('b',32));
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('b',32));
    PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,'expected_generation',1,
        'installation_id','23500000-0000-0000-0000-000000000099'),repeat('c',32));
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',f.run_id),repeat('c',32));
    BEGIN
        PERFORM trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
            'dispatch_id','23500000-0000-0000-0000-000000000552','expected_attempt',1,
            'recovery_policy','patch','input_snapshot',f.input_snapshot),repeat('c',32));
        RAISE EXCEPTION 'a new process trusted the previous generation retry proof';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    PERFORM trellis.mutate('authorize_tool_retry',p,repeat('c',32));
    result:=trellis.mutate('begin_tool_execution',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'dispatch_id','23500000-0000-0000-0000-000000000552','expected_attempt',1,
        'recovery_policy','patch','input_snapshot',f.input_snapshot),repeat('c',32));
    ASSERT result#>>'{tool_call,dispatch_attempt}'='2' AND result#>>'{tool_call,dispatch_generation}'='2';
    SELECT * INTO f FROM reconciliation_fixture WHERE n=59;
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'expected_dispatch_id',f.dispatch_id,
        'expected_attempt',1,'reconciliation','unchanged_original','proof',f.proof);
    PERFORM trellis.mutate('authorize_tool_retry',p,repeat('b',32));
    PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',f.run_id),repeat('b',32));
    PERFORM trellis.mutate('claim_recovery',jsonb_build_object('run_id',f.run_id,'expected_generation',1,
        'installation_id','23500000-0000-0000-0000-000000000099'),repeat('c',32));
    PERFORM trellis.mutate('finish_recovery',jsonb_build_object('run_id',f.run_id),repeat('c',32));
    result:=trellis.mutate('record_tool_result',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
        'content','The original approved desired state is present.','status','completed',
        'result_source','reconciled_desired_state','result_snapshot',jsonb_build_object(
            'source','reconciled_desired_state','reconciliation_proof',jsonb_set(f.proof,'{observed_sha256}',f.proof->'after_sha256'))),repeat('c',32));
    ASSERT result#>>'{tool_call,status}'='completed' AND result#>>'{tool_call,dispatch_attempt}'='1',
        'a fresh desired-state proof must reconcile an authorized retry after a second restart';
    FOR f IN SELECT * FROM reconciliation_fixture WHERE n IN (56,58) LOOP
        p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'expected_dispatch_id',f.dispatch_id,
            'expected_attempt',1,'reconciliation','unchanged_original','proof',f.proof);
        PERFORM trellis.mutate('authorize_tool_retry',p,repeat('b',32));
        IF f.n=56 THEN
            PERFORM trellis.mutate('mark_tool_outcome_unknown',jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,
                'reason','The original patch state changed before its authorized retry.'),repeat('b',32));
        END IF;
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',f.run_id,
            'next_status','interrupted','event_type','run.interrupted'),repeat('b',32));
        ASSERT (SELECT status='outcome_unknown' FROM trellis.tool_calls WHERE id=f.tool_id);
        ASSERT (SELECT NOT retry_allowed AND uncertain_tool_call_id=f.tool_id FROM trellis.runs WHERE id=f.run_id);
        ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE tool_call_id=f.tool_id);
    END LOOP;
END $$;
DO $$
DECLARE f record; p jsonb; result jsonb; metadata jsonb;
BEGIN
    SELECT * INTO f FROM reconciliation_fixture WHERE n=57;
    p:=jsonb_build_object('run_id',f.run_id,'tool_call_id',f.tool_id,'content','Observed file contents.',
        'status','completed','result_source','executor','result_snapshot',jsonb_build_object('padding',repeat('x',1048576)));
    BEGIN
        PERFORM trellis.mutate('record_tool_result',p,repeat('b',32));
        RAISE EXCEPTION 'oversized result metadata was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
    ASSERT NOT EXISTS(SELECT 1 FROM trellis.run_messages WHERE tool_call_id=f.tool_id);
    ASSERT (SELECT status='running' FROM trellis.tool_calls WHERE id=f.tool_id);
    metadata:=jsonb_build_object('padding',repeat('x',1048448));
    p:=jsonb_set(p,'{result_snapshot}',metadata);
    result:=trellis.mutate('record_tool_result',p,repeat('b',32));
    ASSERT result#>'{tool_call,result_snapshot}'=metadata,'bounded metadata near 1 MiB remains supported';
    ASSERT trellis.mutate('record_tool_result',p,repeat('b',32))=result;
END $$;
RESET ROLE;
UPDATE trellis.tool_calls SET retry_receipt=jsonb_build_object('padding',repeat('x',9*1024*1024)),
    dispatch_receipt=jsonb_build_object('padding',repeat('x',9*1024*1024))
    WHERE id=(SELECT tool_id FROM reconciliation_fixture WHERE n=56);
SET LOCAL ROLE authenticated;
DO $$
DECLARE snapshot jsonb;
BEGIN
    snapshot:=trellis.read_recovery_snapshot((SELECT run_id FROM reconciliation_fixture WHERE n=56));
    ASSERT octet_length(snapshot::text)<32768,'retry receipts must be excluded before bounding the canonical snapshot';
    ASSERT NOT (snapshot#>'{tool_calls,0}' ? 'retry_generation') AND NOT (snapshot#>'{tool_calls,0}' ? 'retry_receipt');
    ASSERT NOT has_column_privilege('authenticated','trellis.tool_calls','retry_owner_digest','SELECT');
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_local_reconciliation(text,jsonb,text)','EXECUTE');
END $$;
SELECT set_config('request.jwt.claim.sub','23500000-0000-0000-0000-000000000002',true);
DO $$
BEGIN
    BEGIN
        PERFORM trellis.mutate('record_tool_result',current_setting('trellis.test_reconciled_result')::jsonb,repeat('b',32));
        RAISE EXCEPTION 'another account read reconciled patch observations';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
ROLLBACK;
