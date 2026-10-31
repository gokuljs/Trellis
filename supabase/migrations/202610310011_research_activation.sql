BEGIN;

CREATE OR REPLACE FUNCTION trellis_private.current_research_policy() RETURNS text
LANGUAGE sql IMMUTABLE SET search_path='' AS $$ SELECT 'bounded-research-v2'::text $$;
REVOKE ALL ON FUNCTION trellis_private.current_research_policy() FROM PUBLIC,anon,authenticated;

CREATE OR REPLACE FUNCTION trellis.research_readiness() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path='' AS $$
    SELECT jsonb_build_object('schema_version',2,'policy_version','bounded-research-v2','ready',
        trellis_private.current_research_policy()='bounded-research-v2'
        AND to_regclass('trellis.research_handoff_holds') IS NOT NULL
        AND to_regprocedure('trellis_private.research_mutate_before_handoffs(text,jsonb,text)') IS NOT NULL
        AND to_regprocedure('trellis_private.research_mutate_before_quality(text,jsonb,text)') IS NOT NULL
        AND to_regprocedure('trellis_private.research_task_notebook(jsonb,jsonb)') IS NOT NULL
        AND to_regprocedure('trellis_private.research_budget_snapshot(uuid)') IS NOT NULL
        AND to_regprocedure('trellis_private.research_read_observation(trellis.run_messages,trellis.tool_calls,trellis.web_sources)') IS NOT NULL
        AND to_regprocedure('trellis_private.research_requirement_supported(text,jsonb,jsonb,jsonb,jsonb,jsonb,jsonb)') IS NOT NULL
        AND (SELECT count(*)=2 FROM pg_catalog.pg_attribute
            WHERE attrelid='trellis.runs'::regclass AND NOT attisdropped
                AND attname IN ('research_policy_version','execution_budget_seconds'))
        AND (SELECT count(*)=5 FROM pg_catalog.pg_trigger
            WHERE NOT tgisinternal AND tgenabled IN ('O','A') AND
                (tgrelid='trellis.runs'::regclass AND tgname IN ('preserve_research_profile','release_root_research_handoffs')
                OR tgrelid='trellis.research_tasks'::regclass AND tgname='release_task_research_handoffs'
                OR tgrelid='trellis.research_runs'::regclass AND tgname='release_unknown_research_handoffs'
                OR tgrelid='trellis.tool_calls'::regclass AND tgname='research_source_tool_policy')))
$$;
REVOKE ALL ON FUNCTION trellis.research_readiness() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION trellis.research_readiness() TO anon,authenticated;

COMMIT;
