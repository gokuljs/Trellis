import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiosqlite

from app.domain.models import (
    Message,
    ModelDescriptor,
    ModelId,
    OnboardingProgress,
    OnboardingStep,
    ProviderName,
    Session,
    UserProfile,
)
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
    is_lifecycle_event,
    is_terminal_run_status,
    transition_model_call,
    validate_run_event_transition,
)

SCHEMA_VERSION = 5
TURN_CLAIM_TTL = timedelta(minutes=5)

MIGRATION_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    email TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    selected_provider TEXT NOT NULL CHECK (selected_provider IN ('openai', 'anthropic')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
ON sessions(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    provider TEXT CHECK (provider IN ('openai', 'anthropic')),
    model TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, ordinal),
    UNIQUE(session_id, turn_id, role)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_ordinal
ON messages(session_id, ordinal);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS turn_claims (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS models (
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

INSERT OR IGNORE INTO models(
    id, provider_id, provider_name, adapter_kind, upstream_model_id, name,
    requires_api_key, supports_streaming, supports_tools, enabled, created_at, updated_at
) VALUES
    ('openai:gpt-5.5', 'openai', 'OpenAI', 'openai', 'gpt-5.5', 'GPT-5.5', 1, 1, 0, 1,
     STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'), STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('anthropic:claude-sonnet-5', 'anthropic', 'Anthropic', 'anthropic',
     'claude-sonnet-5', 'Claude Sonnet 5', 1, 1, 0, 1,
     STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'), STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'));

DROP TABLE IF EXISTS app_settings_v3;
CREATE TABLE app_settings_v3 (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    selected_provider TEXT NOT NULL,
    selected_model_id TEXT NOT NULL REFERENCES models(id),
    updated_at TEXT NOT NULL
);
INSERT INTO app_settings_v3(id, selected_provider, selected_model_id, updated_at)
SELECT id, selected_provider,
       CASE selected_provider
           WHEN 'anthropic' THEN 'anthropic:claude-sonnet-5'
           ELSE 'openai:gpt-5.5'
       END,
       updated_at
FROM app_settings;
DROP TABLE app_settings;
ALTER TABLE app_settings_v3 RENAME TO app_settings;

DROP TABLE IF EXISTS messages_v3;
CREATE TABLE messages_v3 (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, ordinal),
    UNIQUE(session_id, turn_id, role)
);
INSERT INTO messages_v3(
    id, session_id, turn_id, ordinal, role, content, provider, model, created_at
)
SELECT id, session_id, turn_id, ordinal, role, content, provider, model, created_at
FROM messages;
DROP TABLE messages;
ALTER TABLE messages_v3 RENAME TO messages;
CREATE INDEX IF NOT EXISTS idx_messages_session_ordinal
ON messages(session_id, ordinal);
"""

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS onboarding_progress (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    flow_version INTEGER NOT NULL,
    current_step TEXT NOT NULL CHECK (
        current_step IN ('intro', 'profile', 'model', 'complete')
    ),
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS runs (
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
    finished_at TEXT,
    UNIQUE(session_id, client_request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_original_per_turn
ON runs(session_id, turn_id) WHERE retry_of IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_per_session
ON runs(session_id) WHERE status IN ('queued', 'running', 'cancelling');
CREATE INDEX IF NOT EXISTS idx_runs_status_created
ON runs(status, created_at);

ALTER TABLE messages ADD COLUMN run_id TEXT REFERENCES runs(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS model_calls (
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
    finished_at TEXT,
    UNIQUE(run_id, step_index)
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence)
);
"""

MIGRATIONS = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
    5: SCHEMA_V5,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._prepare_parent_directory)
        async with self._connect() as connection:
            await connection.executescript(MIGRATION_TABLE_SCHEMA)
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            )
            row = await cursor.fetchone()
            current_version = 0 if row is None else int(row[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    "This database was created by a newer Trellis version. "
                    "Update Trellis to open it."
                )
            for version in range(current_version + 1, SCHEMA_VERSION + 1):
                migration = MIGRATIONS[version]
                await connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    {migration}
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES ({version}, STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'));
                    COMMIT;
                    """
                )
            now = utc_now()
            await connection.execute(
                """
                INSERT INTO users(id, display_name, email, created_at, updated_at)
                SELECT ?, NULL, NULL, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM users)
                """,
                (str(uuid4()), now, now),
            )
            await connection.execute(
                """
                INSERT OR IGNORE INTO app_settings(
                    id, selected_provider, selected_model_id, updated_at
                ) VALUES (1, 'openai', 'openai:gpt-5.5', ?)
                """,
                (now,),
            )
            await connection.execute(
                """
                INSERT OR IGNORE INTO onboarding_progress(
                    user_id, flow_version, current_step, completed_at, created_at, updated_at
                )
                SELECT id, 1,
                       CASE WHEN display_name IS NOT NULL AND email IS NOT NULL
                            THEN 'complete' ELSE 'intro' END,
                       CASE WHEN display_name IS NOT NULL AND email IS NOT NULL
                            THEN ? ELSE NULL END,
                       ?, ?
                FROM users
                """,
                (now, now, now),
            )
            await connection.commit()

    async def get_profile(self) -> UserProfile:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT id, display_name, email, created_at, updated_at FROM users LIMIT 1"
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Trellis profile has not been initialized")
        return UserProfile(
            id=row["id"],
            display_name=row["display_name"],
            email=row["email"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def update_profile(self, display_name: str | None, email: str | None) -> UserProfile:
        now = utc_now()
        async with self._connect() as connection:
            await connection.execute(
                "UPDATE users SET display_name = ?, email = ?, updated_at = ?",
                (display_name, email, now),
            )
            await connection.commit()
        return await self.get_profile()

    async def get_onboarding_progress(self) -> OnboardingProgress:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT current_step FROM onboarding_progress LIMIT 1"
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Trellis onboarding has not been initialized")
        return OnboardingProgress(
            current_step=cast(OnboardingStep, row["current_step"]),
            completed=row["current_step"] == "complete",
        )

    async def advance_onboarding_intro(self) -> OnboardingProgress:
        now = utc_now()
        async with self._connect() as connection:
            await connection.execute(
                """UPDATE onboarding_progress SET current_step = 'profile', updated_at = ?
                   WHERE current_step = 'intro'""",
                (now,),
            )
            await connection.commit()
        return await self.get_onboarding_progress()

    async def save_onboarding_profile(
        self, display_name: str, email: str
    ) -> tuple[UserProfile, OnboardingProgress]:
        now = utc_now()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "UPDATE users SET display_name = ?, email = ?, updated_at = ?",
                (display_name, email, now),
            )
            await connection.execute(
                """UPDATE onboarding_progress
                   SET current_step = CASE WHEN current_step = 'profile'
                                           THEN 'model' ELSE current_step END,
                       updated_at = ?""",
                (now,),
            )
            await connection.commit()
        return await self.get_profile(), await self.get_onboarding_progress()

    async def complete_onboarding(self, model_id: ModelId) -> bool:
        now = utc_now()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            model_cursor = await connection.execute(
                "SELECT provider_id FROM models WHERE id = ? AND enabled = 1", (model_id,)
            )
            model = await model_cursor.fetchone()
            progress_cursor = await connection.execute(
                "SELECT user_id, current_step FROM onboarding_progress LIMIT 1"
            )
            progress = await progress_cursor.fetchone()
            if (
                model is None
                or progress is None
                or progress["current_step"]
                not in {
                    "model",
                    "complete",
                }
            ):
                await connection.rollback()
                return False
            await connection.execute(
                """UPDATE app_settings SET selected_provider = ?, selected_model_id = ?,
                   updated_at = ? WHERE id = 1""",
                (model["provider_id"], model_id, now),
            )
            await connection.execute(
                """UPDATE onboarding_progress SET current_step = 'complete', completed_at = ?,
                   updated_at = ? WHERE user_id = ?""",
                (now, now, progress["user_id"]),
            )
            await connection.commit()
        return True

    async def get_selected_provider(self) -> ProviderName:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT selected_provider FROM app_settings WHERE id = 1"
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Trellis settings have not been initialized")
        return row["selected_provider"]

    async def set_selected_provider(self, provider: ProviderName) -> ProviderName:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """SELECT id FROM models WHERE provider_id = ? AND enabled = 1
                   ORDER BY rowid LIMIT 1""",
                (provider,),
            )
            model = await cursor.fetchone()
            if model is None:
                return provider
            await connection.execute(
                """UPDATE app_settings SET selected_provider = ?, selected_model_id = ?,
                   updated_at = ? WHERE id = 1""",
                (provider, model["id"], utc_now()),
            )
            await connection.commit()
        return provider

    async def get_selected_model_id(self) -> ModelId:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT selected_model_id FROM app_settings WHERE id = 1"
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Trellis settings have not been initialized")
        return row["selected_model_id"]

    async def list_models(self) -> list[ModelDescriptor]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """SELECT id, provider_id, provider_name, adapter_kind, upstream_model_id,
                          name, requires_api_key, supports_streaming, supports_tools, enabled
                   FROM models WHERE enabled = 1 ORDER BY rowid"""
            )
            rows = await cursor.fetchall()
        return [
            ModelDescriptor(
                id=row["id"],
                provider_id=row["provider_id"],
                provider_name=row["provider_name"],
                adapter_kind=row["adapter_kind"],
                upstream_model_id=row["upstream_model_id"],
                name=row["name"],
                requires_api_key=bool(row["requires_api_key"]),
                supports_streaming=bool(row["supports_streaming"]),
                supports_tools=bool(row["supports_tools"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    async def set_selected_model(self, model_id: ModelId) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT provider_id FROM models WHERE id = ? AND enabled = 1", (model_id,)
            )
            model = await cursor.fetchone()
            if model is None:
                return False
            await connection.execute(
                """UPDATE app_settings SET selected_provider = ?, selected_model_id = ?,
                   updated_at = ? WHERE id = 1""",
                (model["provider_id"], model_id, utc_now()),
            )
            await connection.commit()
        return True

    async def create_run(
        self,
        session_id: str,
        turn_id: str,
        client_request_id: str,
        content: str,
        model: ModelDescriptor,
    ) -> RunSnapshot:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("run input cannot be empty")
        if not client_request_id or len(client_request_id) > 200:
            raise ValueError("client request ID must contain 1 to 200 characters")

        now = utc_now()
        deadline = (datetime.now(UTC) + timedelta(seconds=180)).isoformat().replace("+00:00", "Z")
        run_id = str(uuid4())
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            session_cursor = await connection.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_id,)
            )
            if await session_cursor.fetchone() is None:
                await connection.rollback()
                raise ValueError("session not found")

            existing_cursor = await connection.execute(
                """SELECT * FROM runs
                   WHERE session_id = ? AND (
                       client_request_id = ? OR (turn_id = ? AND retry_of IS NULL)
                   )
                   ORDER BY CASE WHEN client_request_id = ? THEN 0 ELSE 1 END
                   LIMIT 1""",
                (session_id, client_request_id, turn_id, client_request_id),
            )
            existing = await existing_cursor.fetchone()
            if existing is not None:
                message_cursor = await connection.execute(
                    "SELECT content FROM messages WHERE id = ?", (existing["input_message_id"],)
                )
                input_message = await message_cursor.fetchone()
                if (
                    existing["turn_id"] != turn_id
                    or input_message is None
                    or input_message["content"] != normalized_content
                    or existing["provider_id"] != model.provider_id
                    or existing["model_id"] != model.id
                    or existing["adapter_kind"] != model.adapter_kind
                    or existing["upstream_model_id"] != model.upstream_model_id
                ):
                    await connection.rollback()
                    raise ValueError("run request conflicts with a previous payload")
                await connection.commit()
                return self._run_from_row(existing)

            active_cursor = await connection.execute(
                """SELECT id FROM runs WHERE session_id = ?
                   AND status IN ('queued', 'running', 'cancelling') LIMIT 1""",
                (session_id,),
            )
            if await active_cursor.fetchone() is not None:
                await connection.rollback()
                raise ValueError("active run already exists for this session")

            user_cursor = await connection.execute(
                """SELECT id, content FROM messages
                   WHERE session_id = ? AND turn_id = ? AND role = 'user'""",
                (session_id, turn_id),
            )
            existing_user = await user_cursor.fetchone()
            if existing_user is not None:
                if existing_user["content"] != normalized_content:
                    await connection.rollback()
                    raise ValueError("run request conflicts with a previous payload")
                input_message_id = existing_user["id"]
            else:
                ordinal_cursor = await connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM messages WHERE session_id = ?",
                    (session_id,),
                )
                ordinal_row = await ordinal_cursor.fetchone()
                if ordinal_row is None:
                    await connection.rollback()
                    raise RuntimeError("could not allocate a message ordinal")
                input_message_id = str(uuid4())
                await connection.execute(
                    """INSERT INTO messages(
                           id, session_id, turn_id, ordinal, role, content, created_at
                       ) VALUES (?, ?, ?, ?, 'user', ?, ?)""",
                    (
                        input_message_id,
                        session_id,
                        turn_id,
                        ordinal_row[0],
                        normalized_content,
                        now,
                    ),
                )
                title = " ".join(normalized_content.split())[:80] or "New session"
                await connection.execute(
                    """UPDATE sessions
                       SET title = CASE WHEN ? = 1 THEN ? ELSE title END, updated_at = ?
                       WHERE id = ?""",
                    (ordinal_row[0], title, now, session_id),
                )

            await connection.execute(
                """INSERT INTO runs(
                       id, session_id, turn_id, input_message_id, client_request_id,
                       retry_of, status, provider_id, model_id, adapter_kind,
                       upstream_model_id, max_model_calls, max_tool_calls, deadline_at,
                       last_event_sequence, created_at
                   ) VALUES (?, ?, ?, ?, ?, NULL, 'queued', ?, ?, ?, ?, 1, 0, ?, 1, ?)""",
                (
                    run_id,
                    session_id,
                    turn_id,
                    input_message_id,
                    client_request_id,
                    model.provider_id,
                    model.id,
                    model.adapter_kind,
                    model.upstream_model_id,
                    deadline,
                    now,
                ),
            )
            event_data = json.dumps(
                {"session_id": session_id, "turn_id": turn_id, "status": "queued"},
                separators=(",", ":"),
            )
            await connection.execute(
                """INSERT INTO run_events(
                       run_id, sequence, event_type, event_version, data, created_at
                   )
                   VALUES (?, 1, ?, 1, ?, ?)""",
                (run_id, RunEventType.QUEUED.value, event_data, now),
            )
            await connection.execute(
                "UPDATE messages SET run_id = ? WHERE id = ?", (run_id, input_message_id)
            )
            run_cursor = await connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
            run_row = await run_cursor.fetchone()
            await connection.commit()
        if run_row is None:
            raise RuntimeError("created run could not be loaded")
        return self._run_from_row(run_row)

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        async with self._connect() as connection:
            cursor = await connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
            row = await cursor.fetchone()
        return None if row is None else self._run_from_row(row)

    async def append_run_event(
        self,
        run_id: str,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        event_version: int = 1,
    ) -> RunEvent:
        now = utc_now()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT status, last_event_sequence FROM runs WHERE id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                raise ValueError("run not found")
            if is_lifecycle_event(event_type):
                await connection.rollback()
                raise ValueError("run lifecycle events must use transition_run_record")
            if is_terminal_run_status(RunStatus(row["status"])):
                await connection.rollback()
                raise ValueError("cannot append an event to a terminal run")
            sequence = int(row["last_event_sequence"]) + 1
            serialized = json.dumps(data, separators=(",", ":"))
            await connection.execute(
                """INSERT INTO run_events(
                       run_id, sequence, event_type, event_version, data, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, sequence, event_type.value, event_version, serialized, now),
            )
            await connection.execute(
                "UPDATE runs SET last_event_sequence = ? WHERE id = ?", (sequence, run_id)
            )
            await connection.commit()
        return RunEvent(run_id, sequence, event_type, event_version, data, now)

    async def list_run_events(
        self,
        run_id: str,
        after_sequence: int = 0,
        *,
        limit: int = 500,
    ) -> list[RunEvent]:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("event cursor and limit are outside the supported range")
        async with self._connect() as connection:
            cursor = await connection.execute(
                """SELECT run_id, sequence, event_type, event_version, data, created_at
                   FROM run_events WHERE run_id = ? AND sequence > ?
                   ORDER BY sequence LIMIT ?""",
                (run_id, after_sequence, limit),
            )
            rows = await cursor.fetchall()
        return [
            RunEvent(
                run_id=row["run_id"],
                sequence=row["sequence"],
                event_type=RunEventType(row["event_type"]),
                event_version=row["event_version"],
                data=cast(dict[str, object], json.loads(row["data"])),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def transition_run_record(
        self,
        run_id: str,
        next_status: RunStatus,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        stop_reason: str | None = None,
    ) -> RunEvent:
        now = utc_now()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                raise ValueError("run not found")
            current_status = RunStatus(row["status"])
            validate_run_event_transition(current_status, next_status, event_type)
            sequence = int(row["last_event_sequence"]) + 1
            started_at = row["started_at"] or (now if next_status is RunStatus.RUNNING else None)
            finished_at = (
                now
                if next_status
                in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                    RunStatus.INTERRUPTED,
                }
                else None
            )
            serialized = json.dumps(data, separators=(",", ":"))
            await connection.execute(
                """UPDATE runs SET status = ?, started_at = ?, finished_at = ?,
                   error_code = ?, error_message = ?, stop_reason = ?, last_event_sequence = ?
                   WHERE id = ?""",
                (
                    next_status.value,
                    started_at,
                    finished_at,
                    error_code,
                    error_message,
                    stop_reason,
                    sequence,
                    run_id,
                ),
            )
            await connection.execute(
                """INSERT INTO run_events(
                       run_id, sequence, event_type, event_version, data, created_at
                   ) VALUES (?, ?, ?, 1, ?, ?)""",
                (run_id, sequence, event_type.value, serialized, now),
            )
            await connection.commit()
        return RunEvent(run_id, sequence, event_type, 1, data, now)

    async def create_model_call(
        self,
        run_id: str,
        step_index: int,
        model: ModelDescriptor,
        request_snapshot: dict[str, object],
    ) -> ModelCallRecord:
        if step_index < 1:
            raise ValueError("model call step index must be positive")
        call_id = str(uuid4())
        now = utc_now()
        async with self._connect() as connection:
            cursor = await connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,))
            if await cursor.fetchone() is None:
                raise ValueError("run not found")
            await connection.execute(
                """INSERT INTO model_calls(
                       id, run_id, step_index, provider_id, model_id, adapter_kind,
                       status, request_snapshot, started_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    call_id,
                    run_id,
                    step_index,
                    model.provider_id,
                    model.id,
                    model.adapter_kind,
                    json.dumps(request_snapshot, separators=(",", ":")),
                    now,
                ),
            )
            await connection.commit()
            call_cursor = await connection.execute(
                "SELECT * FROM model_calls WHERE id = ?", (call_id,)
            )
            row = await call_cursor.fetchone()
        if row is None:
            raise RuntimeError("created model call could not be loaded")
        return self._model_call_from_row(row)

    async def update_model_call(
        self,
        call_id: str,
        next_status: ModelCallStatus,
        *,
        response_snapshot: dict[str, object] | None = None,
        provider_response_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cached_tokens: int | None = None,
        estimated_cost: float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ModelCallRecord:
        now = utc_now()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT status FROM model_calls WHERE id = ?", (call_id,)
            )
            current = await cursor.fetchone()
            if current is None:
                await connection.rollback()
                raise ValueError("model call not found")
            try:
                transition_model_call(ModelCallStatus(current["status"]), next_status)
            except ValueError:
                await connection.rollback()
                raise
            serialized = (
                None
                if response_snapshot is None
                else json.dumps(response_snapshot, separators=(",", ":"))
            )
            finished_at = None if next_status is ModelCallStatus.STREAMING else now
            await connection.execute(
                """UPDATE model_calls SET status = ?,
                   response_snapshot = COALESCE(?, response_snapshot),
                   provider_response_id = COALESCE(?, provider_response_id),
                   finish_reason = COALESCE(?, finish_reason),
                   input_tokens = COALESCE(?, input_tokens),
                   output_tokens = COALESCE(?, output_tokens),
                   reasoning_tokens = COALESCE(?, reasoning_tokens),
                   cached_tokens = COALESCE(?, cached_tokens),
                   estimated_cost = COALESCE(?, estimated_cost),
                   error_code = COALESCE(?, error_code),
                   error_message = COALESCE(?, error_message), finished_at = ?
                   WHERE id = ?""",
                (
                    next_status.value,
                    serialized,
                    provider_response_id,
                    finish_reason,
                    input_tokens,
                    output_tokens,
                    reasoning_tokens,
                    cached_tokens,
                    estimated_cost,
                    error_code,
                    error_message,
                    finished_at,
                    call_id,
                ),
            )
            updated_cursor = await connection.execute(
                "SELECT * FROM model_calls WHERE id = ?", (call_id,)
            )
            row = await updated_cursor.fetchone()
            await connection.commit()
        if row is None:
            raise RuntimeError("updated model call could not be loaded")
        return self._model_call_from_row(row)

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM model_calls WHERE run_id = ? ORDER BY step_index", (run_id,)
            )
            rows = await cursor.fetchall()
        return [self._model_call_from_row(row) for row in rows]

    async def create_session(self) -> Session:
        profile = await self.get_profile()
        session_id = str(uuid4())
        now = utc_now()
        async with self._connect() as connection:
            await connection.execute(
                """
                INSERT INTO sessions(id, user_id, title, created_at, updated_at)
                VALUES (?, ?, 'New session', ?, ?)
                """,
                (session_id, profile.id, now, now),
            )
            await connection.commit()
        session = await self.get_session(session_id)
        if session is None:
            raise RuntimeError("Created session could not be loaded")
        return session

    async def list_sessions(self) -> list[Session]:
        profile = await self.get_profile()
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT s.id, s.user_id, s.title, s.created_at, s.updated_at,
                       COUNT(m.id) AS message_count
                FROM sessions AS s
                LEFT JOIN messages AS m ON m.session_id = s.id
                WHERE s.user_id = ?
                GROUP BY s.id
                ORDER BY s.updated_at DESC, s.rowid DESC
                """,
                (profile.id,),
            )
            rows = await cursor.fetchall()
        return [self._session_from_row(row) for row in rows]

    async def get_session(self, session_id: str) -> Session | None:
        profile = await self.get_profile()
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT s.id, s.user_id, s.title, s.created_at, s.updated_at,
                       COUNT(m.id) AS message_count
                FROM sessions AS s
                LEFT JOIN messages AS m ON m.session_id = s.id
                WHERE s.id = ? AND s.user_id = ?
                GROUP BY s.id
                """,
                (session_id, profile.id),
            )
            row = await cursor.fetchone()
        return None if row is None else self._session_from_row(row)

    async def list_messages(self, session_id: str) -> list[Message]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, session_id, turn_id, ordinal, role, content, provider, model,
                       created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY ordinal
                """,
                (session_id,),
            )
            rows = await cursor.fetchall()
        return [self._message_from_row(row) for row in rows]

    async def get_turn_messages(self, session_id: str, turn_id: str) -> list[Message]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, session_id, turn_id, ordinal, role, content, provider, model,
                       created_at
                FROM messages
                WHERE session_id = ? AND turn_id = ?
                ORDER BY ordinal
                """,
                (session_id, turn_id),
            )
            rows = await cursor.fetchall()
        return [self._message_from_row(row) for row in rows]

    async def claim_turn(self, session_id: str, turn_id: str) -> bool:
        claimed_at = utc_now()
        expires_before = (datetime.now(UTC) - TURN_CLAIM_TTL).isoformat().replace("+00:00", "Z")
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "DELETE FROM turn_claims WHERE claimed_at <= ?",
                (expires_before,),
            )
            try:
                await connection.execute(
                    "INSERT INTO turn_claims(session_id, turn_id, claimed_at) VALUES (?, ?, ?)",
                    (session_id, turn_id, claimed_at),
                )
            except aiosqlite.IntegrityError:
                await connection.commit()
                return False
            await connection.commit()
        return True

    async def release_turn(self, session_id: str, turn_id: str) -> None:
        async with self._connect() as connection:
            await connection.execute(
                "DELETE FROM turn_claims WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            )
            await connection.commit()

    async def add_user_message(self, session_id: str, turn_id: str, content: str) -> Message:
        now = utc_now()
        message_id = str(uuid4())
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Could not allocate a message ordinal")
            ordinal = row[0]
            await connection.execute(
                """
                INSERT INTO messages(
                    id, session_id, turn_id, ordinal, role, content, provider, model, created_at
                )
                VALUES (?, ?, ?, ?, 'user', ?, NULL, NULL, ?)
                """,
                (message_id, session_id, turn_id, ordinal, content, now),
            )
            title = " ".join(content.split())[:80] or "New session"
            await connection.execute(
                """
                UPDATE sessions
                SET title = CASE WHEN ? = 1 THEN ? ELSE title END, updated_at = ?
                WHERE id = ?
                """,
                (ordinal, title, now, session_id),
            )
            await connection.commit()
        messages = await self.get_turn_messages(session_id, turn_id)
        return messages[0]

    async def add_assistant_message(
        self,
        session_id: str,
        turn_id: str,
        content: str,
        provider: ProviderName,
        model: str,
    ) -> Message:
        now = utc_now()
        message_id = str(uuid4())
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Could not allocate a message ordinal")
            ordinal = row[0]
            await connection.execute(
                """
                INSERT INTO messages(
                    id, session_id, turn_id, ordinal, role, content, provider, model, created_at
                )
                VALUES (?, ?, ?, ?, 'assistant', ?, ?, ?, ?)
                """,
                (message_id, session_id, turn_id, ordinal, content, provider, model, now),
            )
            await connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            await connection.commit()
        messages = await self.get_turn_messages(session_id, turn_id)
        return messages[-1]

    @staticmethod
    def _model_call_from_row(row: aiosqlite.Row) -> ModelCallRecord:
        response_snapshot = row["response_snapshot"]
        return ModelCallRecord(
            id=row["id"],
            run_id=row["run_id"],
            step_index=row["step_index"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            adapter_kind=row["adapter_kind"],
            status=ModelCallStatus(row["status"]),
            request_snapshot=cast(dict[str, object], json.loads(row["request_snapshot"])),
            response_snapshot=(
                None
                if response_snapshot is None
                else cast(dict[str, object], json.loads(response_snapshot))
            ),
            provider_response_id=row["provider_response_id"],
            finish_reason=row["finish_reason"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            cached_tokens=row["cached_tokens"],
            estimated_cost=row["estimated_cost"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _run_from_row(row: aiosqlite.Row) -> RunSnapshot:
        return RunSnapshot(
            id=row["id"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            status=RunStatus(row["status"]),
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            adapter_kind=row["adapter_kind"],
            upstream_model_id=row["upstream_model_id"],
            input_message_id=row["input_message_id"],
            retry_of=row["retry_of"],
            client_request_id=row["client_request_id"],
            max_model_calls=row["max_model_calls"],
            max_tool_calls=row["max_tool_calls"],
            deadline_at=row["deadline_at"],
            cancel_requested_at=row["cancel_requested_at"],
            lease_expires_at=row["lease_expires_at"],
            recovery_count=row["recovery_count"],
            stop_reason=row["stop_reason"],
            last_event_sequence=row["last_event_sequence"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _session_from_row(row: aiosqlite.Row) -> Session:
        return Session(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=row["message_count"],
        )

    @staticmethod
    def _message_from_row(row: aiosqlite.Row) -> Message:
        return Message(
            id=row["id"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            ordinal=row["ordinal"],
            role=row["role"],
            content=row["content"],
            provider=row["provider"],
            model=row["model"],
            created_at=row["created_at"],
        )

    def _prepare_parent_directory(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path, timeout=5) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
