\set ON_ERROR_STOP on
DO $check$
BEGIN
    IF has_table_privilege('trellis_runtime_worker','trellis.runs','SELECT')
       OR has_table_privilege('trellis_runtime_worker','trellis.profiles','SELECT')
       OR has_function_privilege('authenticated','trellis_private.require_worker()','EXECUTE')
       OR has_function_privilege('trellis_runtime_worker',
            'trellis_private.mutate_owned(uuid,text,jsonb,text)','EXECUTE') THEN
        RAISE EXCEPTION 'worker privilege boundary is open';
    END IF;
    BEGIN
        PERFORM trellis_private.require_worker();
        RAISE EXCEPTION 'admin session impersonated worker';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END;
$check$;
