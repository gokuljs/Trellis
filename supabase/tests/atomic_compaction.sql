\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id) VALUES
    ('23300000-0000-0000-0000-000000000001'),('23300000-0000-0000-0000-000000000002');
INSERT INTO trellis.profiles(id) VALUES
    ('23300000-0000-0000-0000-000000000001'),('23300000-0000-0000-0000-000000000002');
INSERT INTO trellis.chats(id,user_id) SELECT
    ('23300000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
    '23300000-0000-0000-0000-000000000001'::uuid FROM generate_series(30,35) n;
INSERT INTO trellis.messages(id,user_id,chat_id,turn_id,ordinal,role,content) SELECT
    ('23300000-0000-0000-0000-'||lpad((n*10+o)::text,12,'0'))::uuid,
    '23300000-0000-0000-0000-000000000001'::uuid,
    ('23300000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,'history',o,
    CASE WHEN o=1 THEN 'user' ELSE 'assistant' END,'Historical text'
    FROM generate_series(30,35) n CROSS JOIN generate_series(1,2) o;
CREATE TEMP TABLE compaction_fixture(n integer PRIMARY KEY,payload jsonb NOT NULL,native_call_id uuid);
GRANT SELECT,INSERT ON compaction_fixture TO authenticated;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','23300000-0000-0000-0000-000000000001',true);
DO $$
DECLARE p jsonb; r jsonb; c jsonb; native jsonb; intent jsonb; sources jsonb; draft jsonb; completion jsonb; native_payload jsonb; native_receipt jsonb;
BEGIN
    FOR n IN 30..35 LOOP
        p:=jsonb_build_object('session_id','23300000-0000-0000-0000-'||lpad(n::text,12,'0'),
            'turn_id','current','client_request_id','current','content','Continue',
            'request_fingerprint',repeat('a',64),'budget_preset','conservative',
            'model_id',(SELECT id FROM trellis.models WHERE provider_id='openai' AND enabled ORDER BY catalog_order LIMIT 1),
            'recovery_contract',jsonb_build_object('version',1,'runtime_version','checkpoint-runtime-v1',
                'adapter_versions',jsonb_build_object('openai','v1'),'tool_versions','{}'::jsonb,
                'request_identity','current','installation_id','23300000-0000-0000-0000-000000000099','workspace',NULL));
        r:=trellis.mutate('create_run',p,repeat('a',32));
        PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',r#>>'{run,id}',
            'next_status','running','event_type','run.started'),repeat('a',32));
        sources:=jsonb_build_array(
            jsonb_build_object('source_kind','message','source_id','23300000-0000-0000-0000-'||lpad((n*10+1)::text,12,'0'),'turn_id','history'),
            jsonb_build_object('source_kind','message','source_id','23300000-0000-0000-0000-'||lpad((n*10+2)::text,12,'0'),'turn_id','history'));
        intent:=jsonb_build_object('version',1,
            'operation_id','23300000-0000-0000-0000-'||lpad((n*10+4)::text,12,'0'),
            'target_checkpoint_id','23300000-0000-0000-0000-'||lpad((n*10+3)::text,12,'0'),
            'expected_revision',0,'parent_checkpoint_id',NULL,'sources',sources,
            'source_hash',repeat('a',64),'input_hash',repeat('b',64),
            'instruction_hash',repeat('c',64),'workspace_hash',repeat('d',64),
            'input_tokens_before',200,'pricing_context_tokens',100,'native_model_call_id',NULL,
            'prefix_length',2,'generation_plan','{}'::jsonb,'summary_plan','{}'::jsonb);
        native:=NULL;
        IF n IN (31,32) THEN
            native:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}','step_index',1,
                'model_id',p->>'model_id','request_snapshot',jsonb_build_object('purpose','native_compaction','compaction_intent',intent)),repeat('a',32));
            PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',native->>'id','next_status','streaming'),repeat('a',32));
            native_payload:=jsonb_build_object('run_id',r#>>'{run,id}','call_id',native->>'id',
                'next_status',CASE WHEN n=31 THEN 'completed' ELSE 'failed' END,
                'completion',jsonb_build_object('response_snapshot',jsonb_build_object('native_kind','openai_window','native_payload_json',
                    CASE WHEN n=31 THEN '[{"type":"compaction","encrypted_content":"saved-result"}]' ELSE NULL END),
                    'input_tokens',30,'output_tokens',10,'cached_read_tokens',0,'cache_creation_tokens',0,
                    'accounted_total_tokens',40,'estimated_cost',0.01,
                    'error_code',CASE WHEN n=32 THEN 'native_context_invalid' ELSE NULL END));
            BEGIN
                PERFORM trellis.mutate('commit_native_compaction',native_payload#-'{completion,accounted_total_tokens}',repeat('a',32));
                RAISE EXCEPTION 'unresolved native usage was accepted';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
            IF n=31 THEN
                BEGIN
                    PERFORM trellis.mutate('commit_native_compaction',jsonb_set(native_payload,
                        '{completion,response_snapshot,native_payload_json}','"[]"'),repeat('a',32));
                    RAISE EXCEPTION 'empty native compaction payload was accepted';
                EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
                BEGIN
                    PERFORM trellis.mutate('commit_native_compaction',jsonb_set(jsonb_set(native_payload,
                        '{completion,response_snapshot,native_kind}','"anthropic_block"'),
                        '{completion,response_snapshot,native_payload_json}','"{\"type\":\"compaction\"}"'),repeat('a',32));
                    RAISE EXCEPTION 'native payload from another adapter was accepted';
                EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
            ELSE
                BEGIN
                    PERFORM trellis.mutate('commit_native_compaction',jsonb_set(native_payload,
                        '{completion,error_code}','"native_usage_unknown"'),repeat('a',32));
                    RAISE EXCEPTION 'unknown native outcome was accepted as known fallback';
                EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
            END IF;
            native_receipt:=trellis.mutate('commit_native_compaction',native_payload,repeat('a',32));
            ASSERT trellis.mutate('commit_native_compaction',native_payload,repeat('a',32))=native_receipt,
                'lost native completion acknowledgment must return the saved outcome';
            ASSERT jsonb_array_length(native_receipt->'events')=CASE WHEN n=31 THEN 1 ELSE 0 END;
            BEGIN
                PERFORM trellis.mutate('commit_native_compaction',jsonb_set(native_payload,'{completion,response_snapshot,native_payload_json}','"[]"'),repeat('a',32));
                RAISE EXCEPTION 'native completion receipt was replaced';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
            BEGIN
                PERFORM trellis.mutate('commit_native_compaction',native_payload,repeat('b',32));
                RAISE EXCEPTION 'another capability read a native completion receipt';
            EXCEPTION WHEN insufficient_privilege THEN NULL; END;
            intent:=jsonb_set(intent,'{native_model_call_id}',native->'id');
        END IF;
        IF n=34 THEN intent:=jsonb_set(intent,'{parent_checkpoint_id}','"23300000-0000-0000-0000-000000000999"'); END IF;
        c:=trellis.mutate('create_model_call',jsonb_build_object('run_id',r#>>'{run,id}',
            'step_index',CASE WHEN native IS NULL THEN 1 ELSE 2 END,
            'model_id',p->>'model_id','request_snapshot',jsonb_build_object('purpose','summary','compaction_intent',intent)),repeat('a',32));
        PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',c->>'id','next_status','streaming'),repeat('a',32));
        draft:=jsonb_build_object('id',intent->>'target_checkpoint_id','model_call_id',c->>'id','sources',sources,
            'summary','A portable summary','source_hash',repeat('a',64),'input_hash',repeat('b',64),
            'instruction_hash',repeat('c',64),'workspace_hash',repeat('d',64),
            'input_tokens_before',200,'input_tokens_after',50,'pricing_context_tokens',100,
            'native_kind',CASE WHEN n=31 THEN 'openai_window' ELSE NULL END,
            'native_payload_json',CASE WHEN n=31 THEN '[{"type":"compaction","encrypted_content":"saved-result"}]' ELSE NULL END);
        completion:=jsonb_build_object('response_snapshot',jsonb_build_object('content','A portable summary','usage_context','{}'::jsonb),
            'input_tokens',100,'output_tokens',20,'cached_read_tokens',0,'cache_creation_tokens',0,
            'accounted_total_tokens',120,'estimated_cost',0.02);
        INSERT INTO compaction_fixture VALUES(n,jsonb_build_object('run_id',r#>>'{run,id}','call_id',c->>'id',
            'completion',completion,'expected_revision',0,'draft',draft),(native->>'id')::uuid);
        IF n=35 THEN
            PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',r#>>'{run,id}'),repeat('a',32));
        END IF;
    END LOOP;
END $$;
RESET ROLE;
UPDATE trellis.chats SET context_checkpoint_revision=1 WHERE id='23300000-0000-0000-0000-000000000033';
SET LOCAL ROLE authenticated;
DO $$
DECLARE fixture record; result jsonb; snapshot jsonb; count_before integer; changed jsonb;
BEGIN
    FOR fixture IN SELECT * FROM compaction_fixture ORDER BY n LOOP
        count_before:=(SELECT count(*) FROM trellis.run_events WHERE run_id=(fixture.payload->>'run_id')::uuid);
        IF fixture.n IN (33,34,35) THEN
            BEGIN
                PERFORM trellis.mutate('commit_summary',fixture.payload,repeat('a',32));
                RAISE EXCEPTION 'inconsistent or cancelled compaction committed';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
            ASSERT (SELECT status='streaming' AND accounted_total_tokens IS NULL
                FROM trellis.model_calls WHERE id=(fixture.payload->>'call_id')::uuid),
                'failed checkpoint publication must roll back model completion and usage';
            ASSERT NOT EXISTS(SELECT 1 FROM trellis.context_checkpoints WHERE id=(fixture.payload#>>'{draft,id}')::uuid);
            ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=(fixture.payload->>'run_id')::uuid)=count_before;
            CONTINUE;
        END IF;
        FOREACH changed IN ARRAY ARRAY[
            jsonb_set(fixture.payload,'{draft,id}','"23300000-0000-0000-0000-000000000998"'),
            jsonb_set(fixture.payload,'{draft,source_hash}',to_jsonb(repeat('e',64))),
            jsonb_set(fixture.payload,'{draft,instruction_hash}',to_jsonb(repeat('e',64))),
            jsonb_set(fixture.payload,'{draft,sources,0,turn_id}','"changed"'),
            jsonb_set(fixture.payload,'{expected_revision}','1'),
            jsonb_set(fixture.payload,'{completion,clear_accounted_total_tokens}','true')#-'{completion,accounted_total_tokens}',
            jsonb_set(fixture.payload,'{completion,clear_estimated_cost}','true')#-'{completion,estimated_cost}'
        ] LOOP
            BEGIN
                PERFORM trellis.mutate('commit_summary',changed,repeat('a',32));
                RAISE EXCEPTION 'compaction intent changed at commit';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        END LOOP;
        IF fixture.n=31 THEN
            BEGIN
                PERFORM trellis.mutate('commit_summary',jsonb_set(fixture.payload,'{draft,native_payload_json}','"[]"'),repeat('a',32));
                RAISE EXCEPTION 'native compaction result was replaced';
            EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        END IF;
        result:=trellis.mutate('commit_summary',fixture.payload,repeat('a',32));
        ASSERT result#>>'{model_call,status}'='completed' AND result#>>'{model_call,purpose}'='summary';
        ASSERT result#>>'{model_call,accounted_total_tokens}'='120';
        ASSERT result#>>'{checkpoint,id}'=fixture.payload#>>'{draft,id}';
        ASSERT result#>>'{checkpoint,model_call_id}'=fixture.payload->>'call_id';
        ASSERT result#>>'{checkpoint,revision}'='1' AND result#>'{checkpoint,parent_id}'='null'::jsonb;
        ASSERT result#>>'{checkpoint,summary}'='A portable summary';
        ASSERT jsonb_array_length(result->'events')=1 AND result#>>'{events,0,event_type}'='model.completed';
        ASSERT result#>>'{events,0,data,purpose}'='summary';
        ASSERT (SELECT count(*) FROM trellis.context_checkpoints WHERE id=(fixture.payload#>>'{draft,id}')::uuid)=1;
        ASSERT trellis.mutate('commit_summary',fixture.payload,repeat('a',32))=result,'lost acknowledgment must reuse the original checkpoint and receipt';
        ASSERT (SELECT count(*) FROM trellis.run_events WHERE run_id=(fixture.payload->>'run_id')::uuid)=count_before+1;
        BEGIN
            PERFORM trellis.mutate('commit_summary',jsonb_set(fixture.payload,'{completion,output_tokens}','21'),repeat('a',32));
            RAISE EXCEPTION 'summary receipt content changed';
        EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
        BEGIN
            PERFORM trellis.mutate('commit_summary',fixture.payload,repeat('b',32));
            RAISE EXCEPTION 'another capability read the summary receipt';
        EXCEPTION WHEN insufficient_privilege THEN NULL; END;
        snapshot:=trellis.read_recovery_snapshot((fixture.payload->>'run_id')::uuid);
        ASSERT (SELECT count(*) FROM trellis.model_calls WHERE run_id=(fixture.payload->>'run_id')::uuid)=
            CASE WHEN fixture.n=30 THEN 1 ELSE 2 END,'native compaction must not be dispatched again';
        IF fixture.n=31 THEN
            ASSERT result#>>'{checkpoint,native_payload_json}'='[{"type":"compaction","encrypted_content":"saved-result"}]';
            ASSERT snapshot#>>'{model_calls,0,status}'='completed';
        ELSIF fixture.n=32 THEN
            ASSERT result#>'{checkpoint,native_payload_json}'='null'::jsonb;
            ASSERT snapshot#>>'{model_calls,0,status}'='failed';
            ASSERT snapshot#>>'{model_calls,0,accounted_total_tokens}'='40','known native failure usage remains charged';
        END IF;
        IF fixture.n=30 THEN
            PERFORM trellis.mutate('relinquish_run',jsonb_build_object('run_id',fixture.payload->>'run_id'),repeat('a',32));
            ASSERT trellis.mutate('commit_summary',fixture.payload,repeat('a',32))=result,
                'saved summary receipt remains readable after ownership expires';
        END IF;
    END LOOP;
END $$;
SELECT set_config('request.jwt.claim.sub','23300000-0000-0000-0000-000000000002',true);
DO $$
DECLARE p jsonb;
BEGIN
    SELECT payload INTO p FROM compaction_fixture WHERE n=30;
    BEGIN
        PERFORM trellis.mutate('commit_summary',p,repeat('a',32));
        RAISE EXCEPTION 'another account read the saved summary receipt';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    ASSERT NOT has_function_privilege('authenticated','trellis_private.mutate_before_atomic_compaction(text,jsonb,text)','EXECUTE');
END $$;
ROLLBACK;
