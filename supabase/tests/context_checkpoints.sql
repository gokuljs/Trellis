\set ON_ERROR_STOP on
BEGIN;
INSERT INTO auth.users(id,email) VALUES
 ('00000000-0000-0000-0000-0000000006a1','checkpoint-a@example.test'),
 ('00000000-0000-0000-0000-0000000006b1','checkpoint-b@example.test');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000006a1',true);
SELECT trellis.ensure_account();
DO $check$
DECLARE
    chat uuid; run uuid; call uuid; old_user uuid; old_assistant uuid;
    result jsonb; payload jsonb; saved jsonb; child jsonb;
    checkpoint uuid := gen_random_uuid(); child_id uuid := gen_random_uuid();
    cap text := 'CheckpointCapabilityLongEnoughForLeaseProtection123';
    native text := '[ { "type" : "compaction", "encrypted_content" : "opaque" }, {"type":"message","content":"kept"} ]';
BEGIN
    result:=trellis.mutate('create_session','{}'::jsonb); chat:=(result->>'id')::uuid;
    result:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','old',
        'client_request_id','old','content','Remember the preference','model_id','openai:gpt-5.5'),cap);
    run:=(result->'run'->>'id')::uuid; old_user:=(result->'run'->>'input_message_id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','running','event_type','run.started'),cap);
    result:=trellis.mutate('complete_run',jsonb_build_object('run_id',run,'content','I will remember'),cap);
    old_assistant:=(result->'message'->>'id')::uuid;
    result:=trellis.mutate('create_run',jsonb_build_object('session_id',chat,'turn_id','now',
        'client_request_id','now','content','Continue','model_id','openai:gpt-5.5'),cap);
    run:=(result->'run'->>'id')::uuid;
    PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run,
        'next_status','running','event_type','run.started'),cap);
    result:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',1,
        'model_id','openai:gpt-5.5','request_snapshot',jsonb_build_object('purpose','summary')),cap);
    call:=(result->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','completed'),cap);
    payload:=jsonb_build_object('id',checkpoint,'run_id',run,'model_call_id',call,'expected_revision',0,
        'sources',jsonb_build_array(jsonb_build_object('source_id',old_user,'source_kind','message','turn_id','old'),
            jsonb_build_object('source_id',old_assistant,'source_kind','message','turn_id','old')),
        'summary','The user has a preference.','source_hash',repeat('a',64),'input_hash',repeat('b',64),
        'instruction_hash',repeat('c',64),'workspace_hash',repeat('d',64),
        'input_tokens_before',1000,'input_tokens_after',100,'pricing_context_tokens',300000,
        'native_kind','openai_window','native_payload_json',native);
    saved:=trellis.mutate('save_context_checkpoint',payload,cap);
    IF saved->>'revision' IS DISTINCT FROM '1' OR saved->>'covered_message_ordinal' IS DISTINCT FROM '2' THEN
        RAISE EXCEPTION 'checkpoint was not installed atomically';
    END IF;
    IF saved->>'native_payload_json' IS DISTINCT FROM native OR saved->>'pricing_context_tokens' IS DISTINCT FROM '300000' THEN
        RAISE EXCEPTION 'native payload or pricing context changed';
    END IF;
    IF trellis.mutate('save_context_checkpoint',payload,cap) IS DISTINCT FROM saved THEN
        RAISE EXCEPTION 'retry changed installed checkpoint';
    END IF;
    IF (SELECT count(*) FROM trellis.messages WHERE chat_id=chat)<>3 THEN
        RAISE EXCEPTION 'checkpoint deleted transcript';
    END IF;
    BEGIN
        PERFORM trellis.mutate('save_context_checkpoint',jsonb_set(payload,'{id}',to_jsonb(child_id)),cap);
        RAISE EXCEPTION 'stale checkpoint revision accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('save_context_checkpoint',jsonb_set(payload,'{summary}','"changed"'::jsonb),cap);
        RAISE EXCEPTION 'conflicting checkpoint retry accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('save_context_checkpoint',payload,'IncorrectCheckpointCapabilityLongEnoughForProtection123');
        RAISE EXCEPTION 'foreign capability wrote checkpoint';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        UPDATE trellis.context_checkpoints SET summary='forged' WHERE id=checkpoint;
        RAISE EXCEPTION 'direct checkpoint mutation accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        PERFORM trellis.mutate('save_context_checkpoint',payload || jsonb_build_object('id',child_id,'expected_revision',1,
            'sources',jsonb_build_array(jsonb_build_object('source_id',old_assistant,'source_kind','message','turn_id','old'))),cap);
        RAISE EXCEPTION 'checkpoint rehydrated archived transcript';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    result:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run,'step_index',2,
        'model_id','openai:gpt-5.5','request_snapshot',jsonb_build_object('purpose','summary')),cap);
    call:=(result->>'id')::uuid;
    PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call,'next_status','completed'),cap);
    child:=trellis.mutate('save_context_checkpoint',payload || jsonb_build_object('id',child_id,
        'model_call_id',call,'expected_revision',1,'summary','A newer summary.','pricing_context_tokens',1000,
        'sources',jsonb_build_array(jsonb_build_object('source_id',checkpoint,'source_kind','checkpoint','turn_id','checkpoint'))),cap);
    IF child->>'revision' IS DISTINCT FROM '2' OR child->>'parent_id' IS DISTINCT FROM checkpoint::text OR
       child->>'pricing_context_tokens' IS DISTINCT FROM '300000' THEN
        RAISE EXCEPTION 'checkpoint lineage or pricing high-water mark lost';
    END IF;
    IF (SELECT summary FROM trellis.context_checkpoints WHERE id=checkpoint) IS DISTINCT FROM 'The user has a preference.' THEN
        RAISE EXCEPTION 'previous checkpoint was overwritten';
    END IF;
    result:=trellis.read_context_window(chat,128);
    IF result->'checkpoint'->>'id' IS DISTINCT FROM child_id::text
       OR jsonb_array_length(result->'messages')<>1
       OR result->'messages'->0->>'content' IS DISTINCT FROM 'Continue' THEN
        RAISE EXCEPTION 'context snapshot paired checkpoint with incorrect tail';
    END IF;
    BEGIN
        PERFORM trellis.read_context_window(chat,129);
        RAISE EXCEPTION 'unbounded context snapshot accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    PERFORM trellis.mutate('request_run_cancellation',jsonb_build_object('run_id',run),cap);
    BEGIN
        PERFORM trellis.mutate('save_context_checkpoint',payload || jsonb_build_object('id',gen_random_uuid(),'expected_revision',2),cap);
        RAISE EXCEPTION 'cancelling run installed checkpoint';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL; END;
    IF (SELECT context_checkpoint_revision FROM trellis.chats WHERE id=chat)<>2 OR
       (SELECT count(*) FROM trellis.context_checkpoints WHERE chat_id=chat)<>2 THEN
        RAISE EXCEPTION 'failed save changed checkpoint state';
    END IF;
END;
$check$;
SELECT set_config('request.jwt.claim.sub','00000000-0000-0000-0000-0000000006b1',true);
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM trellis.context_checkpoints) THEN RAISE EXCEPTION 'other account read checkpoints'; END IF;
END; $$;
ROLLBACK;
