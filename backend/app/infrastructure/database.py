import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

from app.domain.models import Message, ProviderName, Session, UserProfile

SCHEMA_VERSION = 1

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


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._prepare_parent_directory)
        async with self._connect() as connection:
            await connection.executescript(SCHEMA_V1)
            now = utc_now()
            await connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, now),
            )
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
                INSERT OR IGNORE INTO app_settings(id, selected_provider, updated_at)
                VALUES (1, 'openai', ?)
                """,
                (now,),
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
            await connection.execute(
                "UPDATE app_settings SET selected_provider = ?, updated_at = ? WHERE id = 1",
                (provider, utc_now()),
            )
            await connection.commit()
        return provider

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
