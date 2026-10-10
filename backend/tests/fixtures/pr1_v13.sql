-- Frozen PR1 per-account SQLite schema, version 13. Retained for one-time import tests.
BEGIN TRANSACTION;
CREATE TABLE "app_settings" (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    selected_provider TEXT NOT NULL,
    selected_model_id TEXT NOT NULL REFERENCES models(id),
    updated_at TEXT NOT NULL
, default_budget_preset TEXT NOT NULL
    DEFAULT 'conservative'
    CHECK (default_budget_preset IN ('conservative', 'longer')));
CREATE TABLE "messages" (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    created_at TEXT NOT NULL, run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    UNIQUE(session_id, ordinal),
    UNIQUE(session_id, turn_id, role)
);
CREATE TABLE model_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    adapter_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'streaming', 'completed', 'failed', 'cancelled', 'timed_out')
    ),
    request_snapshot TEXT NOT NULL,
    response_snapshot TEXT,
    provider_response_id TEXT,
    finish_reason TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cached_tokens INTEGER,
    estimated_cost REAL,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT, cache_creation_tokens INTEGER,
    UNIQUE(run_id, step_index)
);
CREATE TABLE models (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    adapter_kind TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    name TEXT NOT NULL,
    requires_api_key INTEGER NOT NULL CHECK (requires_api_key IN (0, 1)),
    supports_streaming INTEGER NOT NULL CHECK (supports_streaming IN (0, 1)),
    supports_tools INTEGER NOT NULL CHECK (supports_tools IN (0, 1)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO "models" VALUES('openai:gpt-5.5','openai','OpenAI','openai','gpt-5.5','GPT-5.5',1,1,1,1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
INSERT INTO "models" VALUES('anthropic:claude-sonnet-5','anthropic','Anthropic','anthropic','claude-sonnet-5','Claude Sonnet 5',1,1,1,1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
CREATE TABLE onboarding_progress (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    flow_version INTEGER NOT NULL,
    current_step TEXT NOT NULL CHECK (
        current_step IN ('intro', 'profile', 'model', 'complete')
    ),
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE run_events (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence)
);
CREATE TABLE run_messages (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    role TEXT NOT NULL CHECK (role IN ('assistant', 'tool')),
    content TEXT NOT NULL,
    continuation_json TEXT NOT NULL DEFAULT '[]',
    model_call_id TEXT REFERENCES model_calls(id),
    tool_call_id TEXT REFERENCES tool_calls(id),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, ordinal),
    UNIQUE(model_call_id),
    UNIQUE(tool_call_id),
    CHECK (
        (role = 'assistant' AND model_call_id IS NOT NULL AND tool_call_id IS NULL)
        OR (role = 'tool' AND model_call_id IS NULL AND tool_call_id IS NOT NULL)
    )
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    input_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    client_request_id TEXT NOT NULL,
    retry_of TEXT REFERENCES runs(id),
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'cancelling', 'completed', 'failed', 'cancelled', 'interrupted'
        )
    ),
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    adapter_kind TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    max_model_calls INTEGER NOT NULL,
    max_tool_calls INTEGER NOT NULL,
    deadline_at TEXT NOT NULL,
    cancel_requested_at TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    recovery_count INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    error_code TEXT,
    error_message TEXT,
    last_event_sequence INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT, budget_preset TEXT NOT NULL DEFAULT 'legacy'
    CHECK (budget_preset IN ('legacy', 'conservative', 'longer')), max_total_tokens INTEGER NOT NULL DEFAULT 100000, max_cost_usd REAL NOT NULL DEFAULT 2.0, waiting_tool_call_id TEXT REFERENCES tool_calls(id),
    UNIQUE(session_id, client_request_id)
);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO "schema_migrations" VALUES(1,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(2,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(3,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(4,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(5,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(6,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(7,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(8,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(9,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(10,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(11,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(12,'2026-01-01T00:00:00Z');
INSERT INTO "schema_migrations" VALUES(13,'2026-01-01T00:00:00Z');
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, workspace_path TEXT);
CREATE TABLE tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    assistant_message_id TEXT NOT NULL REFERENCES run_messages(id),
    call_index INTEGER NOT NULL CHECK (call_index >= 0),
    provider_call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'denied', 'cancelled', 'timed_out')
    ),
    approval_decision TEXT CHECK (approval_decision IN ('approved', 'denied')),
    approval_decided_at TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT, approval_preview_json TEXT,
    UNIQUE(run_id, provider_call_id),
    UNIQUE(assistant_message_id, call_index)
);
CREATE TABLE turn_claims (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    email TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE workspace_test_presets (
    workspace_path TEXT NOT NULL,
    name TEXT NOT NULL,
    command TEXT NOT NULL,
    cwd TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(workspace_path, name)
);
CREATE INDEX idx_sessions_user_updated
ON sessions(user_id, updated_at DESC);
CREATE INDEX idx_messages_session_ordinal
ON messages(session_id, ordinal);
CREATE UNIQUE INDEX idx_runs_one_original_per_turn
ON runs(session_id, turn_id) WHERE retry_of IS NULL;
CREATE UNIQUE INDEX idx_runs_one_active_per_session
ON runs(session_id) WHERE status IN ('queued', 'running', 'cancelling');
CREATE INDEX idx_runs_status_created
ON runs(status, created_at);
CREATE INDEX idx_run_messages_run_ordinal ON run_messages(run_id, ordinal);
CREATE INDEX idx_tool_calls_run_message ON tool_calls(run_id, assistant_message_id);
COMMIT;
