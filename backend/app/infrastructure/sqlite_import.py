"""One-time, additive import of PR1's private per-account SQLite data.

The caller owns the account flock for the entire import. SQLite is opened read-only;
the original database remains a backup. Cloud writes go through an authenticated,
transactional import RPC supplied by the repository adapter.
"""

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import aiosqlite

from app.domain.runtime import RunEventType

try:
    import fcntl
except ImportError:  # pragma: no cover - the account store itself requires POSIX.
    fcntl = None  # type: ignore[assignment]


MAX_BATCH_ROWS = 100
MAX_BATCH_BYTES = 7_500_000  # Leave room for the RPC envelope under its 8 MiB cap.
MARKER_NAME = ".supabase-imported.json"
_INTERRUPTION = "The run was interrupted while local data moved to cloud storage."
_PHASES = (
    "profiles",
    "user_settings",
    "onboarding_progress",
    "chats",
    "messages",
    "runs",
    "message_run_links",
    "model_calls",
    "run_events",
    "run_messages_assistant",
    "tool_calls",
    "run_messages_tool",
)


class LegacyImportSink(Protocol):
    async def import_batch(self, table: str, rows: list[dict[str, object]]) -> None: ...


@dataclass(frozen=True, slots=True)
class _PendingTool:
    id: str
    sequence: int
    ordinal: int
    message_id: str


@dataclass(frozen=True, slots=True)
class _ActiveRun:
    ended_at: str
    terminal_sequence: int
    pending_tools: tuple[_PendingTool, ...]


def _private_regular(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("Account import source must be a private regular file")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("Account import source must be owned and private")
    return metadata


def _validate_locked_source(
    account_dir: Path, user_id: UUID, lock_fd: int
) -> tuple[Path | None, int]:
    if fcntl is None:
        raise RuntimeError("Account import requires POSIX file locking")
    if account_dir.name != str(user_id) or account_dir.parent.name != "accounts":
        raise RuntimeError("Account import path does not match authenticated user")
    for path in (account_dir.parent, account_dir):
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise RuntimeError("Account import requires a private account directory")
    lock_metadata = _private_regular(account_dir / ".account.lock")
    held_metadata = os.fstat(lock_fd)
    if (lock_metadata.st_dev, lock_metadata.st_ino) != (
        held_metadata.st_dev,
        held_metadata.st_ino,
    ):
        raise RuntimeError("Account import requires the account lock")
    source = account_dir / "state.db"
    if source.exists() or source.is_symlink():
        _private_regular(source)
    else:
        source = None
    held_fd = os.dup(lock_fd)
    try:
        fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return source, held_fd
    except BaseException:
        os.close(held_fd)
        raise


def _decode_json(value: str | None, *, nullable: bool = False) -> object:
    if value is None:
        if nullable:
            return None
        raise RuntimeError("Legacy account data has missing JSON")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError("Legacy account data has invalid JSON") from error


async def _validate_database(connection: aiosqlite.Connection, user_id: UUID) -> None:
    cursor = await connection.execute("PRAGMA quick_check")
    quick_check = await cursor.fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise RuntimeError("Legacy account database failed integrity check")
    cursor = await connection.execute("SELECT MAX(version) FROM schema_migrations")
    version = await cursor.fetchone()
    if version is None or version[0] != 13:
        raise RuntimeError("Legacy account database must have PR1 schema version 13")
    cursor = await connection.execute("SELECT id FROM users LIMIT 2")
    owners = tuple(await cursor.fetchall())
    if len(owners) != 1 or owners[0]["id"] != str(user_id):
        raise RuntimeError("Legacy account database owner does not match authenticated user")
    cursor = await connection.execute("PRAGMA foreign_key_check")
    if await cursor.fetchone() is not None:
        raise RuntimeError("Legacy account database has broken foreign keys")
    cursor = await connection.execute(
        "SELECT 1 FROM sessions WHERE user_id != ? LIMIT 1", (str(user_id),)
    )
    if await cursor.fetchone() is not None:
        raise RuntimeError("Legacy account database has another owner's chats")
    cursor = await connection.execute(
        "SELECT 1 FROM onboarding_progress WHERE user_id != ? LIMIT 1", (str(user_id),)
    )
    if await cursor.fetchone() is not None:
        raise RuntimeError("Legacy account database has another owner's onboarding")
    ownership_checks = (
        """SELECT 1 FROM runs AS r JOIN messages AS m ON m.id = r.input_message_id
           WHERE r.session_id != m.session_id LIMIT 1""",
        """SELECT 1 FROM messages AS m JOIN runs AS r ON r.id = m.run_id
           WHERE m.session_id != r.session_id LIMIT 1""",
        """SELECT 1 FROM run_messages AS m
           JOIN model_calls AS c ON c.id = m.model_call_id
           WHERE m.run_id != c.run_id LIMIT 1""",
        """SELECT 1 FROM run_messages AS m
           JOIN tool_calls AS t ON t.id = m.tool_call_id
           WHERE m.run_id != t.run_id LIMIT 1""",
        """SELECT 1 FROM tool_calls AS t
           JOIN run_messages AS m ON m.id = t.assistant_message_id
           WHERE t.run_id != m.run_id LIMIT 1""",
        """SELECT 1 FROM runs AS r
           JOIN tool_calls AS t ON t.id = r.waiting_tool_call_id
           WHERE r.id != t.run_id LIMIT 1""",
        """SELECT 1 FROM runs AS r JOIN runs AS previous ON previous.id = r.retry_of
           WHERE r.session_id != previous.session_id LIMIT 1""",
        """SELECT 1 FROM tool_calls AS t JOIN runs AS r ON r.id = t.run_id
           WHERE t.status IN ('pending', 'running')
             AND r.status NOT IN ('queued', 'running', 'cancelling') LIMIT 1""",
        """SELECT 1 FROM model_calls AS c JOIN runs AS r ON r.id = c.run_id
           WHERE c.status IN ('pending', 'streaming')
             AND r.status NOT IN ('queued', 'running', 'cancelling') LIMIT 1""",
    )
    for query in ownership_checks:
        cursor = await connection.execute(query)
        if await cursor.fetchone() is not None:
            raise RuntimeError("Legacy account database has inconsistent run ownership or state")
    cursor = await connection.execute(
        """WITH RECURSIVE ordered(id) AS (
             SELECT id FROM runs WHERE retry_of IS NULL
             UNION ALL
             SELECT r.id FROM runs AS r JOIN ordered AS parent ON r.retry_of = parent.id
           )
           SELECT (SELECT COUNT(*) FROM ordered), (SELECT COUNT(*) FROM runs)"""
    )
    counts = await cursor.fetchone()
    if counts is None or counts[0] != counts[1]:
        raise RuntimeError("Legacy account run retries are not a valid history")


async def _active_runs(connection: aiosqlite.Connection, user_id: UUID) -> dict[str, _ActiveRun]:
    cursor = await connection.execute(
        """SELECT r.id, r.last_event_sequence,
                  COALESCE((SELECT MAX(e.sequence) FROM run_events AS e
                            WHERE e.run_id = r.id), 0) AS actual_sequence,
                  COALESCE((SELECT MAX(e.created_at) FROM run_events AS e
                            WHERE e.run_id = r.id), r.started_at, r.created_at) AS ended_at,
                  COALESCE((SELECT MAX(m.ordinal) FROM run_messages AS m
                            WHERE m.run_id = r.id), 0) AS last_ordinal
           FROM runs AS r JOIN sessions AS s ON s.id = r.session_id
           WHERE s.user_id = ? AND r.status IN ('queued', 'running', 'cancelling')
           ORDER BY r.id""",
        (str(user_id),),
    )
    active: dict[str, _ActiveRun] = {}
    async for run in cursor:
        if run["actual_sequence"] != run["last_event_sequence"]:
            raise RuntimeError("Legacy account run has an inconsistent event sequence")
        tool_cursor = await connection.execute(
            """SELECT t.id, EXISTS(
                   SELECT 1 FROM run_messages AS result WHERE result.tool_call_id = t.id
               ) AS has_result FROM tool_calls AS t
               JOIN run_messages AS m ON m.id = t.assistant_message_id
               WHERE t.run_id = ? AND t.status IN ('pending', 'running')
               ORDER BY m.ordinal, t.call_index""",
            (run["id"],),
        )
        pending = await tool_cursor.fetchall()
        if any(tool["has_result"] for tool in pending):
            raise RuntimeError("Legacy account has a pending tool with a saved result")
        tools = tuple(
            _PendingTool(
                id=tool["id"],
                sequence=int(run["last_event_sequence"]) + index + 1,
                ordinal=int(run["last_ordinal"]) + index + 1,
                message_id=str(
                    uuid5(NAMESPACE_URL, f"trellis:import:{user_id}:{tool['id']}:interrupted")
                ),
            )
            for index, tool in enumerate(pending)
        )
        active[run["id"]] = _ActiveRun(
            ended_at=run["ended_at"],
            terminal_sequence=int(run["last_event_sequence"]) + len(tools) + 1,
            pending_tools=tools,
        )
    return active


_QUERIES = {
    "profiles": "SELECT id, display_name, created_at, updated_at FROM users ORDER BY id",
    "user_settings": """SELECT selected_provider, selected_model_id, default_budget_preset,
                              updated_at FROM app_settings ORDER BY id""",
    "onboarding_progress": """SELECT user_id, flow_version, current_step, completed_at,
                                    created_at, updated_at FROM onboarding_progress
                                    ORDER BY user_id""",
    "chats": """SELECT id, user_id, title, workspace_path, created_at, updated_at
                FROM sessions ORDER BY id""",
    "messages": """SELECT m.id, s.user_id, m.session_id AS chat_id, m.turn_id, m.ordinal,
                          m.role, m.content, m.provider, m.model, m.created_at
                   FROM messages AS m JOIN sessions AS s ON s.id = m.session_id
                   ORDER BY m.id""",
    "runs": """WITH RECURSIVE ordered(id, depth) AS (
                  SELECT id, 0 FROM runs WHERE retry_of IS NULL
                  UNION ALL
                  SELECT r.id, parent.depth + 1 FROM runs AS r
                  JOIN ordered AS parent ON r.retry_of = parent.id
              )
              SELECT r.*, s.user_id FROM ordered AS o
              JOIN runs AS r ON r.id = o.id
              JOIN sessions AS s ON s.id = r.session_id
              ORDER BY o.depth, r.created_at, r.id""",
    "message_run_links": """SELECT m.id, m.run_id, s.user_id FROM messages AS m
                            JOIN sessions AS s ON s.id = m.session_id
                            WHERE m.run_id IS NOT NULL ORDER BY m.id""",
    "model_calls": """SELECT m.*, s.user_id FROM model_calls AS m
                       JOIN runs AS r ON r.id = m.run_id
                       JOIN sessions AS s ON s.id = r.session_id ORDER BY m.id""",
    "run_events": """SELECT e.*, s.user_id FROM run_events AS e
                      JOIN runs AS r ON r.id = e.run_id
                      JOIN sessions AS s ON s.id = r.session_id
                      ORDER BY e.run_id, e.sequence""",
    "run_messages_assistant": """SELECT m.*, s.user_id FROM run_messages AS m
                                 JOIN runs AS r ON r.id = m.run_id
                                 JOIN sessions AS s ON s.id = r.session_id
                                 WHERE m.role = 'assistant' ORDER BY m.id""",
    "tool_calls": """SELECT t.*, s.user_id FROM tool_calls AS t
                    JOIN runs AS r ON r.id = t.run_id
                    JOIN sessions AS s ON s.id = r.session_id ORDER BY t.id""",
    "run_messages_tool": """SELECT m.*, s.user_id FROM run_messages AS m
                            JOIN runs AS r ON r.id = m.run_id
                            JOIN sessions AS s ON s.id = r.session_id
                            WHERE m.role = 'tool' ORDER BY m.id""",
}


def _map_row(
    phase: str, row: aiosqlite.Row, user_id: UUID, active: dict[str, _ActiveRun]
) -> dict[str, object]:
    data = dict(row)
    if phase == "profiles":
        return data
    if phase == "user_settings":
        return {
            "user_id": str(user_id),
            **data,
            "created_at": data["updated_at"],
        }
    if phase == "onboarding_progress" or phase == "chats":
        return data
    if phase == "messages":
        return {**data, "run_id": None, "updated_at": data["created_at"]}
    if phase == "message_run_links":
        return data
    if phase == "runs":
        run_id = str(data["id"])
        state = active.get(run_id)
        data["chat_id"] = data.pop("session_id")
        data["budget_preset"] = (
            "conservative" if data["budget_preset"] == "legacy" else data["budget_preset"]
        )
        data["lease_token"] = None
        data["lease_expires_at"] = None
        data["waiting_tool_call_id"] = None
        if state is not None:
            data.update(
                status="interrupted",
                recovery_count=int(data["recovery_count"]) + 1,
                stop_reason="migration_interrupted",
                error_code="migration_interrupted",
                error_message=_INTERRUPTION,
                last_event_sequence=state.terminal_sequence,
                finished_at=state.ended_at,
            )
        data["updated_at"] = data["finished_at"] or data["started_at"] or data["created_at"]
        return data
    if phase == "model_calls":
        data["request_snapshot"] = _decode_json(data["request_snapshot"])
        data["response_snapshot"] = _decode_json(data["response_snapshot"], nullable=True)
        data["cached_read_tokens"] = data.pop("cached_tokens")
        data["created_at"] = data["started_at"]
        state = active.get(str(data["run_id"]))
        if state is not None and data["status"] in ("pending", "streaming"):
            data.update(
                status="cancelled",
                error_code="migration_interrupted",
                error_message=_INTERRUPTION,
                finished_at=state.ended_at,
            )
        data["updated_at"] = data["finished_at"] or data["created_at"]
        return data
    if phase == "run_events":
        data["data"] = _decode_json(data["data"])
        data["updated_at"] = data["created_at"]
        return data
    if phase.startswith("run_messages"):
        data["continuation"] = _decode_json(data.pop("continuation_json"))
        data["updated_at"] = data["created_at"]
        return data
    if phase == "tool_calls":
        data["arguments"] = _decode_json(data.pop("arguments_json"))
        data["approval_preview"] = _decode_json(data.pop("approval_preview_json"), nullable=True)
        state = active.get(str(data["run_id"]))
        if state is not None and data["status"] in ("pending", "running"):
            data.update(status="cancelled", finished_at=state.ended_at)
        data["updated_at"] = data["finished_at"] or data["created_at"]
        return data
    raise AssertionError(f"Unknown import phase: {phase}")


async def _rows(
    connection: aiosqlite.Connection,
    phase: str,
    user_id: UUID,
    active: dict[str, _ActiveRun],
) -> AsyncIterator[dict[str, object]]:
    cursor = await connection.execute(_QUERIES[phase])
    while chunk := await cursor.fetchmany(MAX_BATCH_ROWS):
        for row in chunk:
            yield _map_row(phase, row, user_id, active)
    if phase == "run_events":
        for run_id, run in active.items():
            for tool in run.pending_tools:
                yield {
                    "user_id": str(user_id),
                    "run_id": run_id,
                    "sequence": tool.sequence,
                    "event_type": RunEventType.TOOL_RESULT.value,
                    "event_version": 1,
                    "data": {
                        "tool_call_id": tool.id,
                        "message_id": tool.message_id,
                        "status": "cancelled",
                        "content": _INTERRUPTION,
                    },
                    "created_at": run.ended_at,
                    "updated_at": run.ended_at,
                }
            yield {
                "user_id": str(user_id),
                "run_id": run_id,
                "sequence": run.terminal_sequence,
                "event_type": RunEventType.INTERRUPTED.value,
                "event_version": 1,
                "data": {"code": "migration_interrupted", "message": _INTERRUPTION},
                "created_at": run.ended_at,
                "updated_at": run.ended_at,
            }
    if phase == "run_messages_tool":
        for run_id, run in active.items():
            for tool in run.pending_tools:
                yield {
                    "id": tool.message_id,
                    "user_id": str(user_id),
                    "run_id": run_id,
                    "ordinal": tool.ordinal,
                    "role": "tool",
                    "content": _INTERRUPTION,
                    "continuation": [],
                    "model_call_id": None,
                    "tool_call_id": tool.id,
                    "created_at": run.ended_at,
                    "updated_at": run.ended_at,
                }


def _encoded_row(phase: str, row: dict[str, object]) -> bytes:
    return (
        json.dumps([phase, row], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        + b"\n"
    )


async def _digest(
    connection: aiosqlite.Connection, user_id: UUID, active: dict[str, _ActiveRun]
) -> str:
    digest = hashlib.sha256()
    for phase in _PHASES:
        async for row in _rows(connection, phase, user_id, active):
            encoded = _encoded_row(phase, row)
            if len(encoded) > MAX_BATCH_BYTES:
                raise RuntimeError("A legacy account row exceeds the import size limit")
            digest.update(encoded)
    return digest.hexdigest()


def _marker_digest(path: Path, user_id: UUID) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    _private_regular(path)
    try:
        marker = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Account import completion marker is invalid") from error
    if (
        not isinstance(marker, dict)
        or marker.get("version") != 1
        or marker.get("user_id") != str(user_id)
        or not isinstance(marker.get("digest"), str)
    ):
        raise RuntimeError("Account import completion marker is invalid")
    return marker["digest"]


def _write_marker(path: Path, user_id: UUID, digest: str) -> None:
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    payload = json.dumps(
        {"version": 1, "user_id": str(user_id), "digest": digest}, sort_keys=True
    ).encode()
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("Could not write account import marker")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _release_unclaimed_source(task: asyncio.Task[tuple[Path | None, int]]) -> None:
    try:
        _, held_fd = task.result()
    except BaseException:
        return
    os.close(held_fd)


async def import_locked_account(
    account_dir: Path, user_id: UUID, lock_fd: int, sink: LegacyImportSink
) -> None:
    """Import a verified user's PR1 database once while its account lock is held.

    A failed or cancelled import leaves no completion marker. Repeating it safely
    replays immutable rows; the RPC detects conflicting cloud data.
    """
    validation = asyncio.create_task(
        asyncio.to_thread(_validate_locked_source, account_dir, user_id, lock_fd)
    )
    try:
        source, held_fd = await asyncio.shield(validation)
    except BaseException:
        validation.add_done_callback(_release_unclaimed_source)
        raise
    marker_task: asyncio.Task[None] | None = None
    try:
        if source is None:
            return
        uri = f"file:{quote(str(source), safe='/')}?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA query_only = ON")
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("BEGIN")
            await _validate_database(connection, user_id)
            active = await _active_runs(connection, user_id)
            digest = await _digest(connection, user_id, active)
            marker_path = account_dir / MARKER_NAME
            previous = await asyncio.to_thread(_marker_digest, marker_path, user_id)
            if previous is not None:
                if previous != digest:
                    raise RuntimeError("Legacy account data changed after cloud import")
                return
            for phase in _PHASES:
                table = phase.removesuffix("_assistant").removesuffix("_tool")
                batch: list[dict[str, object]] = []
                batch_bytes = 0
                async for row in _rows(connection, phase, user_id, active):
                    row_bytes = len(_encoded_row(phase, row))
                    if batch and (
                        len(batch) >= MAX_BATCH_ROWS or batch_bytes + row_bytes > MAX_BATCH_BYTES
                    ):
                        await sink.import_batch(table, batch)
                        batch = []
                        batch_bytes = 0
                    batch.append(row)
                    batch_bytes += row_bytes
                if batch:
                    await sink.import_batch(table, batch)
            marker_task = asyncio.create_task(
                asyncio.to_thread(_write_marker, marker_path, user_id, digest)
            )
            await asyncio.shield(marker_task)
    finally:
        if marker_task is not None and not marker_task.done():
            marker_task.add_done_callback(lambda _: os.close(held_fd))
        else:
            os.close(held_fd)
