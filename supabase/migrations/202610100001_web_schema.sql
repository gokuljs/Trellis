-- Trellis web data. Apply to a Supabase project, then expose only the trellis
-- schema in API settings. The trellis_private schema must stay unexposed.
BEGIN;

CREATE SCHEMA trellis;
CREATE SCHEMA trellis_private;
REVOKE ALL ON SCHEMA trellis FROM PUBLIC, anon;
REVOKE ALL ON SCHEMA trellis_private FROM PUBLIC, anon;
GRANT USAGE ON SCHEMA trellis TO authenticated;

CREATE TABLE trellis.profiles (
    id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    display_name text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trellis.models (
    id text PRIMARY KEY,
    provider_id text NOT NULL,
    provider_name text NOT NULL,
    adapter_kind text NOT NULL,
    upstream_model_id text NOT NULL,
    name text NOT NULL,
    requires_api_key boolean NOT NULL,
    supports_streaming boolean NOT NULL,
    supports_tools boolean NOT NULL,
    enabled boolean NOT NULL,
    catalog_order integer NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider_id, id)
);

CREATE TABLE trellis.user_settings (
    user_id uuid PRIMARY KEY REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    selected_provider text NOT NULL,
    selected_model_id text NOT NULL REFERENCES trellis.models(id),
    default_budget_preset text NOT NULL DEFAULT 'conservative'
        CHECK (default_budget_preset IN ('conservative', 'longer')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (selected_provider, selected_model_id)
        REFERENCES trellis.models(provider_id, id)
);

CREATE TABLE trellis.onboarding_progress (
    user_id uuid PRIMARY KEY REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    flow_version integer NOT NULL DEFAULT 1 CHECK (flow_version > 0),
    current_step text NOT NULL DEFAULT 'intro'
        CHECK (current_step IN ('intro', 'profile', 'model', 'complete')),
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((current_step = 'complete') = (completed_at IS NOT NULL))
);

CREATE TABLE trellis.chats (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    title text NOT NULL DEFAULT 'New session',
    workspace_path text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, id)
);

CREATE TABLE trellis.messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    chat_id uuid NOT NULL,
    turn_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL,
    provider text,
    model text,
    run_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, id),
    UNIQUE (user_id, chat_id, turn_id, id),
    UNIQUE (user_id, chat_id, ordinal),
    UNIQUE (user_id, chat_id, turn_id, role),
    FOREIGN KEY (user_id, chat_id) REFERENCES trellis.chats(user_id, id)
        ON DELETE CASCADE
);

CREATE TABLE trellis.turn_claims (
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    chat_id uuid NOT NULL,
    turn_id text NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, chat_id),
    FOREIGN KEY (user_id, chat_id) REFERENCES trellis.chats(user_id, id)
        ON DELETE CASCADE
);

CREATE TABLE trellis.runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    chat_id uuid NOT NULL,
    turn_id text NOT NULL,
    input_message_id uuid NOT NULL,
    client_request_id text NOT NULL CHECK (length(client_request_id) BETWEEN 1 AND 200),
    retry_of uuid,
    status text NOT NULL CHECK (status IN (
        'queued', 'running', 'waiting_for_approval', 'cancelling',
        'completed', 'failed', 'cancelled', 'interrupted'
    )),
    provider_id text NOT NULL,
    model_id text NOT NULL REFERENCES trellis.models(id),
    adapter_kind text NOT NULL,
    upstream_model_id text NOT NULL,
    budget_preset text NOT NULL CHECK (budget_preset IN ('conservative', 'longer')),
    max_model_calls integer NOT NULL CHECK (max_model_calls > 0),
    max_tool_calls integer NOT NULL CHECK (max_tool_calls > 0),
    max_total_tokens integer NOT NULL CHECK (max_total_tokens > 0),
    max_cost_usd numeric NOT NULL CHECK (max_cost_usd > 0),
    deadline_at timestamptz NOT NULL,
    cancel_requested_at timestamptz,
    lease_token text,
    lease_expires_at timestamptz,
    recovery_count integer NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
    waiting_tool_call_id uuid,
    stop_reason text,
    error_code text,
    error_message text,
    last_event_sequence integer NOT NULL DEFAULT 0 CHECK (last_event_sequence >= 0),
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, id),
    UNIQUE (user_id, chat_id, client_request_id),
    UNIQUE (user_id, chat_id, turn_id, id),
    FOREIGN KEY (user_id, chat_id) REFERENCES trellis.chats(user_id, id)
        ON DELETE CASCADE,
    CONSTRAINT runs_input_message_owner_fk
    FOREIGN KEY (user_id, chat_id, turn_id, input_message_id)
        REFERENCES trellis.messages(user_id, chat_id, turn_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (user_id, chat_id, turn_id, retry_of)
        REFERENCES trellis.runs(user_id, chat_id, turn_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (provider_id, model_id) REFERENCES trellis.models(provider_id, id)
);

ALTER TABLE trellis.messages ADD CONSTRAINT messages_run_owner_fk
    FOREIGN KEY (user_id, chat_id, turn_id, run_id)
    REFERENCES trellis.runs(user_id, chat_id, turn_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE trellis.model_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    run_id uuid NOT NULL,
    step_index integer NOT NULL CHECK (step_index > 0),
    provider_id text NOT NULL,
    model_id text NOT NULL REFERENCES trellis.models(id),
    adapter_kind text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'pending', 'streaming', 'completed', 'failed', 'cancelled', 'timed_out'
    )),
    request_snapshot jsonb NOT NULL,
    response_snapshot jsonb,
    provider_response_id text,
    finish_reason text,
    input_tokens integer CHECK (input_tokens >= 0),
    output_tokens integer CHECK (output_tokens >= 0),
    reasoning_tokens integer CHECK (reasoning_tokens >= 0),
    cached_read_tokens integer CHECK (cached_read_tokens >= 0),
    cache_creation_tokens integer CHECK (cache_creation_tokens >= 0),
    estimated_cost numeric CHECK (estimated_cost >= 0),
    error_code text,
    error_message text,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, id),
    UNIQUE (user_id, run_id, id),
    UNIQUE (user_id, run_id, step_index),
    FOREIGN KEY (user_id, run_id) REFERENCES trellis.runs(user_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (provider_id, model_id) REFERENCES trellis.models(provider_id, id)
);

CREATE TABLE trellis.run_events (
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    run_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    event_version integer NOT NULL DEFAULT 1 CHECK (event_version > 0),
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, run_id, sequence),
    FOREIGN KEY (user_id, run_id) REFERENCES trellis.runs(user_id, id)
        ON DELETE CASCADE
);

CREATE TABLE trellis.run_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    run_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    role text NOT NULL CHECK (role IN ('assistant', 'tool')),
    content text NOT NULL,
    continuation jsonb NOT NULL DEFAULT '[]'::jsonb,
    model_call_id uuid,
    tool_call_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, id),
    UNIQUE (user_id, run_id, id),
    UNIQUE (user_id, run_id, ordinal),
    UNIQUE (model_call_id),
    UNIQUE (tool_call_id),
    CHECK (
        (role = 'assistant' AND model_call_id IS NOT NULL AND tool_call_id IS NULL)
        OR (role = 'tool' AND model_call_id IS NULL AND tool_call_id IS NOT NULL)
    ),
    FOREIGN KEY (user_id, run_id) REFERENCES trellis.runs(user_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (user_id, run_id, model_call_id)
        REFERENCES trellis.model_calls(user_id, run_id, id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE trellis.tool_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES trellis.profiles(id) ON DELETE CASCADE,
    run_id uuid NOT NULL,
    assistant_message_id uuid NOT NULL,
    call_index integer NOT NULL CHECK (call_index >= 0),
    provider_call_id text NOT NULL,
    name text NOT NULL,
    arguments jsonb NOT NULL,
    status text NOT NULL CHECK (status IN (
        'pending', 'running', 'completed', 'failed', 'denied', 'cancelled', 'timed_out'
    )),
    approval_decision text CHECK (approval_decision IN ('approved', 'denied')),
    approval_decided_at timestamptz,
    approval_preview jsonb,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, id),
    UNIQUE (user_id, run_id, id),
    UNIQUE (user_id, run_id, provider_call_id),
    UNIQUE (user_id, assistant_message_id, call_index),
    FOREIGN KEY (user_id, run_id) REFERENCES trellis.runs(user_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (user_id, run_id, assistant_message_id)
        REFERENCES trellis.run_messages(user_id, run_id, id)
        DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE trellis.run_messages ADD CONSTRAINT run_messages_tool_owner_fk
    FOREIGN KEY (user_id, run_id, tool_call_id)
    REFERENCES trellis.tool_calls(user_id, run_id, id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE trellis.runs ADD CONSTRAINT runs_waiting_tool_owner_fk
    FOREIGN KEY (user_id, id, waiting_tool_call_id)
    REFERENCES trellis.tool_calls(user_id, run_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX runs_one_original_per_turn
    ON trellis.runs (user_id, chat_id, turn_id) WHERE retry_of IS NULL;
CREATE UNIQUE INDEX runs_one_active_per_chat
    ON trellis.runs (user_id, chat_id)
    WHERE status IN ('queued', 'running', 'waiting_for_approval', 'cancelling');
CREATE INDEX chats_user_updated ON trellis.chats(user_id, updated_at DESC, id);
CREATE INDEX messages_chat_ordinal ON trellis.messages(user_id, chat_id, ordinal);
CREATE INDEX runs_chat_created ON trellis.runs(user_id, chat_id, created_at, id);
CREATE INDEX runs_lease_expiry ON trellis.runs(lease_expires_at)
    WHERE status IN ('queued', 'running', 'waiting_for_approval', 'cancelling');
CREATE INDEX model_calls_run_step ON trellis.model_calls(user_id, run_id, step_index);
CREATE INDEX run_messages_run_ordinal ON trellis.run_messages(user_id, run_id, ordinal);
CREATE INDEX tool_calls_run_message ON trellis.tool_calls(user_id, run_id, assistant_message_id, call_index);

INSERT INTO trellis.models (
    id, provider_id, provider_name, adapter_kind, upstream_model_id, name,
    requires_api_key, supports_streaming, supports_tools, enabled, catalog_order
) VALUES
    ('openai:gpt-5.5', 'openai', 'OpenAI', 'openai', 'gpt-5.5', 'GPT-5.5',
     true, true, true, true, 1),
    ('anthropic:claude-sonnet-5', 'anthropic', 'Anthropic', 'anthropic',
     'claude-sonnet-5', 'Claude Sonnet 5', true, true, true, true, 2);

CREATE FUNCTION trellis_private.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    NEW.updated_at := pg_catalog.clock_timestamp();
    RETURN NEW;
END
$$;

DO $triggers$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'profiles', 'models', 'user_settings', 'onboarding_progress',
        'chats', 'messages', 'turn_claims', 'runs', 'model_calls',
        'run_events', 'run_messages', 'tool_calls'
    ] LOOP
        EXECUTE pg_catalog.format(
            'CREATE TRIGGER touch_updated_at BEFORE UPDATE ON trellis.%I '
            || 'FOR EACH ROW EXECUTE FUNCTION trellis_private.touch_updated_at()',
            table_name
        );
    END LOOP;
END
$triggers$;

DO $policies$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'profiles', 'models', 'user_settings', 'onboarding_progress',
        'chats', 'messages', 'turn_claims', 'runs', 'model_calls',
        'run_events', 'run_messages', 'tool_calls'
    ] LOOP
        EXECUTE pg_catalog.format('ALTER TABLE trellis.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE pg_catalog.format('ALTER TABLE trellis.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON trellis.%I FROM PUBLIC, anon, authenticated', table_name);
        IF table_name = 'models' THEN
            EXECUTE 'CREATE POLICY models_read ON trellis.models FOR SELECT TO authenticated USING (enabled)';
        ELSIF table_name = 'profiles' THEN
            EXECUTE 'CREATE POLICY profiles_read ON trellis.profiles FOR SELECT TO authenticated '
                    || 'USING ((SELECT auth.uid()) IS NOT NULL AND id = (SELECT auth.uid()))';
        ELSE
            EXECUTE pg_catalog.format(
                'CREATE POLICY %I ON trellis.%I FOR SELECT TO authenticated '
                || 'USING ((SELECT auth.uid()) IS NOT NULL AND user_id = (SELECT auth.uid()))',
                table_name || '_read', table_name
            );
        END IF;
    END LOOP;
END
$policies$;

GRANT SELECT ON trellis.profiles, trellis.models, trellis.user_settings,
    trellis.onboarding_progress, trellis.chats, trellis.messages,
    trellis.turn_claims, trellis.model_calls, trellis.run_events,
    trellis.run_messages, trellis.tool_calls TO authenticated;
-- The digest of the local run capability is never part of a PostgREST row.
GRANT SELECT (
    id, user_id, chat_id, turn_id, input_message_id, client_request_id, retry_of,
    status, provider_id, model_id, adapter_kind, upstream_model_id, budget_preset,
    max_model_calls, max_tool_calls, max_total_tokens, max_cost_usd, deadline_at,
    cancel_requested_at, lease_expires_at, recovery_count, waiting_tool_call_id,
    stop_reason, error_code, error_message, last_event_sequence, started_at,
    finished_at, created_at, updated_at
) ON trellis.runs TO authenticated;

-- Restrict defaults too, so new tables/functions cannot become API-writable
-- because of Supabase's stock grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA trellis REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA trellis REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA trellis_private REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION trellis_private.touch_updated_at() FROM PUBLIC, anon, authenticated;

COMMIT;
