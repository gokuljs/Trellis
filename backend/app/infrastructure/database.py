import asyncio
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

SCHEMA_VERSION = 4
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

MIGRATIONS = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
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
