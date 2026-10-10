import asyncio
import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.infrastructure import sqlite_import
from app.infrastructure.accounts import _acquire_account_lock, _prepare_account_directory
from app.infrastructure.sqlite_import import import_locked_account

PR1_SCHEMA = Path(__file__).parent / "fixtures" / "pr1_v13.sql"


class RecordingSink:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.batches: list[tuple[str, list[dict[str, object]]]] = []
        self.fail_on = fail_on
        self.seal_calls = 0
        self.capabilities: list[str] = []

    async def import_batch(
        self, table: str, rows: list[dict[str, object]], capability: str
    ) -> None:
        self.capabilities.append(capability)
        if table == self.fail_on:
            raise RuntimeError("cloud unavailable")
        self.batches.append((table, rows))

    async def seal_import(self, capability: str) -> None:
        self.capabilities.append(capability)
        if self.fail_on == "seal":
            raise RuntimeError("cloud unavailable")
        self.seal_calls += 1


async def _account(tmp_path: Path, user_id: UUID) -> tuple[Path, int]:
    await asyncio.to_thread(tmp_path.chmod, 0o700)
    account_dir = _prepare_account_directory(tmp_path, user_id)
    with closing(sqlite3.connect(account_dir / "state.db")) as connection, connection:
        connection.executescript(PR1_SCHEMA.read_text())
        now = "2026-01-01T00:00:00Z"
        connection.execute(
            "INSERT INTO users(id, display_name, email, created_at, updated_at) "
            "VALUES (?, NULL, NULL, ?, ?)",
            (str(user_id), now, now),
        )
        connection.execute(
            "INSERT INTO app_settings(id, selected_provider, selected_model_id, updated_at) "
            "VALUES (1, 'openai', 'openai:gpt-5.5', ?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO onboarding_progress("
            "user_id, flow_version, current_step, created_at, updated_at) "
            "VALUES (?, 1, 'intro', ?, ?)",
            (str(user_id), now, now),
        )
    (account_dir / "state.db").chmod(0o600)
    return account_dir, _acquire_account_lock(account_dir)


def _update(path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(sql, parameters)


@pytest.mark.anyio
async def test_import_database_validation_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    validate = sqlite_import._validate_database
    release = threading.Event()

    def pause_validation(connection: sqlite3.Connection, owner: UUID) -> None:
        if not release.wait(1):
            raise RuntimeError("SQLite validation blocked the event loop")
        validate(connection, owner)

    monkeypatch.setattr(sqlite_import, "_validate_database", pause_validation)
    asyncio.get_running_loop().call_later(0.05, release.set)
    try:
        await import_locked_account(account_dir, user_id, lock_fd, RecordingSink())
    finally:
        release.set()
        os.close(lock_fd)


@pytest.mark.anyio
async def test_cancellation_waits_for_sqlite_read_before_closing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    validate = sqlite_import._validate_database
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def pause_validation(connection: sqlite3.Connection, owner: UUID) -> None:
        entered.set()
        if not release.wait(2):
            raise RuntimeError("SQLite validation was not released")
        validate(connection, owner)
        finished.set()

    monkeypatch.setattr(sqlite_import, "_validate_database", pause_validation)
    sink = RecordingSink()
    task = asyncio.create_task(import_locked_account(account_dir, user_id, lock_fd, sink))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert sink.batches == []
        assert not (account_dir / ".supabase-imported.json").exists()
        with closing(sqlite3.connect(account_dir / "state.db")) as connection, connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        release.set()
        os.close(lock_fd)


@pytest.mark.anyio
async def test_imports_owned_conversation_rows_and_skips_completed_replay(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    state = account_dir / "state.db"
    chat_id = uuid4()
    message_id = uuid4()
    now = "2026-01-01T00:00:00Z"
    _update(state, "UPDATE users SET display_name = ?, email = ?", ("Ada", "local@example.test"))
    _update(
        state,
        "INSERT INTO sessions(id,user_id,title,workspace_path,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (str(chat_id), str(user_id), "Project", "/private/local", now, now),
    )
    _update(
        state,
        "INSERT INTO messages(id,session_id,turn_id,ordinal,role,content,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (str(message_id), str(chat_id), "turn-1", 1, "user", "hello", now),
    )
    (account_dir / ".env").write_text("OPENAI_API_KEY=should-not-import\n")
    sink = RecordingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        batches = {name: rows for name, rows in sink.batches}
        assert batches["profiles"][0]["id"] == str(user_id)
        assert batches["profiles"][0]["display_name"] == "Ada"
        assert "email" not in batches["profiles"][0]
        assert batches["chats"][0] == {
            "id": str(chat_id),
            "user_id": str(user_id),
            "title": "Project",
            "workspace_path": "/private/local",
            "created_at": now,
            "updated_at": now,
        }
        assert batches["messages"][0]["id"] == str(message_id)
        assert "turn_claims" not in batches
        assert "workspace_test_presets" not in batches
        assert "should-not-import" not in json.dumps(sink.batches)
        assert len(set(sink.capabilities)) == 1
        assert len(sink.capabilities[0]) >= 32
        assert (account_dir / ".supabase-import-capability").stat().st_mode & 0o777 == 0o600
        count = len(sink.batches)
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert len(sink.batches) == count
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_rejects_mismatched_source_owner_without_sending_rows(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    other_id = uuid4()
    _update(account_dir / "state.db", "UPDATE users SET id = ?", (str(other_id),))
    sink = RecordingSink()
    try:
        with pytest.raises(RuntimeError, match="owner"):
            await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert sink.batches == []
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_failed_batch_is_replayed_without_completion_marker(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    failing_sink = RecordingSink(fail_on="user_settings")
    try:
        with pytest.raises(RuntimeError, match="cloud unavailable"):
            await import_locked_account(account_dir, user_id, lock_fd, failing_sink)
        assert failing_sink.seal_calls == 0
        assert not (account_dir / ".supabase-imported.json").exists()
        sink = RecordingSink()
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert (account_dir / ".supabase-imported.json").exists()
        assert [name for name, _ in sink.batches][:2] == ["profiles", "user_settings"]
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_seals_cloud_import_before_writing_completion_marker(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    marker = account_dir / ".supabase-imported.json"

    class InspectingSink(RecordingSink):
        async def seal_import(self, capability: str) -> None:
            assert marker.exists() is (self.seal_calls > 0)
            assert self.batches
            await super().seal_import(capability)

    sink = InspectingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert sink.seal_calls == 1
        assert marker.exists()
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert sink.seal_calls == 1
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_missing_completion_marker_replays_with_original_private_capability(
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    sink = RecordingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        original_capability = sink.capabilities[0]
        (account_dir / ".supabase-imported.json").unlink()
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert set(sink.capabilities) == {original_capability}
        assert sink.seal_calls == 2
        assert (account_dir / ".supabase-imported.json").exists()
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_missing_source_after_partial_import_requires_restoration(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    sink = RecordingSink(fail_on="user_settings")
    try:
        with pytest.raises(RuntimeError, match="cloud unavailable"):
            await import_locked_account(account_dir, user_id, lock_fd, sink)
        (account_dir / "state.db").unlink()
        with pytest.raises(RuntimeError, match=r"restore the original state\.db"):
            await import_locked_account(account_dir, user_id, lock_fd, RecordingSink())
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_failed_seal_does_not_write_completion_marker(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    sink = RecordingSink(fail_on="seal")
    try:
        with pytest.raises(RuntimeError, match="cloud unavailable"):
            await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert sink.batches
        assert not (account_dir / ".supabase-imported.json").exists()
        assert (account_dir / ".supabase-import-capability").stat().st_mode & 0o777 == 0o600
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_replays_only_existing_rows_after_seal_if_marker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    marker = account_dir / ".supabase-imported.json"

    class ReplayOnlySink(RecordingSink):
        def __init__(self) -> None:
            super().__init__()
            self.sealed = False
            self.accepted: set[str] = set()

        async def import_batch(
            self, table: str, rows: list[dict[str, object]], capability: str
        ) -> None:
            key = json.dumps([table, rows], sort_keys=True)
            if self.sealed and key not in self.accepted:
                raise RuntimeError("new cloud data after seal")
            self.accepted.add(key)
            await super().import_batch(table, rows, capability)

        async def seal_import(self, capability: str) -> None:
            self.sealed = True
            await super().seal_import(capability)

    write_marker = sqlite_import._write_marker
    attempts = 0

    def fail_once(path: Path, owner: UUID, digest: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("marker write failed")
        write_marker(path, owner, digest)

    monkeypatch.setattr(sqlite_import, "_write_marker", fail_once)
    sink = ReplayOnlySink()
    try:
        with pytest.raises(OSError, match="marker write failed"):
            await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert sink.sealed
        assert not marker.exists()
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert marker.exists()
        assert sink.seal_calls == 2
        assert len(set(sink.capabilities)) == 1
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_new_device_without_legacy_database_does_not_seal_import(tmp_path: Path) -> None:
    user_id = uuid4()
    await asyncio.to_thread(tmp_path.chmod, 0o700)
    account_dir = await asyncio.to_thread(_prepare_account_directory, tmp_path, user_id)
    lock_fd = await asyncio.to_thread(_acquire_account_lock, account_dir)
    sink = RecordingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert sink.seal_calls == 0
        assert sink.batches == []
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_changed_source_after_completion_fails_closed(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    sink = RecordingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        _update(account_dir / "state.db", "UPDATE users SET display_name = 'Changed'")
        with pytest.raises(RuntimeError, match="changed after"):
            await import_locked_account(account_dir, user_id, lock_fd, sink)
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_imports_active_run_as_interrupted_without_changing_backup(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    state = account_dir / "state.db"
    chat_id, message_id, run_id, model_call_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = "2026-01-01T00:00:00Z"
    _update(
        state,
        "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
        (str(chat_id), str(user_id), "Run", now, now),
    )
    _update(
        state,
        "INSERT INTO messages(id,session_id,turn_id,ordinal,role,content,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (str(message_id), str(chat_id), "turn", 1, "user", "hello", now),
    )
    _update(
        state,
        """INSERT INTO runs(
             id,session_id,turn_id,input_message_id,client_request_id,status,
             provider_id,model_id,adapter_kind,upstream_model_id,max_model_calls,
             max_tool_calls,deadline_at,created_at,last_event_sequence,lease_token
             ) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,1,?)""",
        (
            str(run_id),
            str(chat_id),
            "turn",
            str(message_id),
            "request",
            "openai",
            "openai:gpt-5.5",
            "openai",
            "gpt-5.5",
            3,
            5,
            now,
            now,
            "local-secret-lease",
        ),
    )
    _update(state, "UPDATE messages SET run_id = ? WHERE id = ?", (str(run_id), str(message_id)))
    _update(
        state,
        """INSERT INTO model_calls(
             id,run_id,step_index,provider_id,model_id,adapter_kind,status,
             request_snapshot,started_at) VALUES (?,?,?,?,?,?,'streaming','{}',?)""",
        (str(model_call_id), str(run_id), 1, "openai", "openai:gpt-5.5", "openai", now),
    )
    _update(
        state,
        "INSERT INTO run_events(run_id,sequence,event_type,event_version,data,created_at) "
        "VALUES (?,1,'run.queued',1,?,?)",
        (str(run_id), json.dumps({"status": "queued"}), now),
    )
    sink = RecordingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        batches = {name: rows for name, rows in sink.batches}
        assert batches["runs"][0]["status"] == "interrupted"
        assert batches["runs"][0]["lease_token"] is None
        assert batches["runs"][0]["last_event_sequence"] == 2
        assert batches["model_calls"][0]["status"] == "cancelled"
        assert batches["message_run_links"] == [
            {"id": str(message_id), "run_id": str(run_id), "user_id": str(user_id)}
        ]
        assert batches["run_events"][-1]["event_type"] == "run.interrupted"
        assert batches["run_events"][-1]["sequence"] == 2
        with closing(sqlite3.connect(state)) as connection, connection:
            assert connection.execute("SELECT status, lease_token FROM runs").fetchone() == (
                "queued",
                "local-secret-lease",
            )
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_limits_import_batches_to_one_hundred_rows(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    state = account_dir / "state.db"
    with closing(sqlite3.connect(state)) as connection, connection:
        connection.executemany(
            "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            ((str(uuid4()), str(user_id), "Chat", "2026-01-01", "2026-01-01") for _ in range(101)),
        )
    sink = RecordingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        chat_batch_sizes = [len(rows) for name, rows in sink.batches if name == "chats"]
        assert chat_batch_sizes == [100, 1]
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_imports_pending_approval_as_cancelled_tool_result(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    state = account_dir / "state.db"
    chat_id, input_id, run_id, model_call_id, assistant_id, tool_id = (uuid4() for _ in range(6))
    now = "2026-01-01T00:00:00Z"
    with closing(sqlite3.connect(state)) as connection, connection:
        connection.execute(
            "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (str(chat_id), str(user_id), "Approval", now, now),
        )
        connection.execute(
            "INSERT INTO messages(id,session_id,turn_id,ordinal,role,content,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(input_id), str(chat_id), "turn", 1, "user", "run", now),
        )
        connection.execute(
            """INSERT INTO runs(
                 id,session_id,turn_id,input_message_id,client_request_id,status,
                 provider_id,model_id,adapter_kind,upstream_model_id,max_model_calls,
                 max_tool_calls,deadline_at,created_at,last_event_sequence
                 ) VALUES (?,?,?,?,?,'running',?,?,?,?,?,?,?,?,1)""",
            (
                str(run_id),
                str(chat_id),
                "turn",
                str(input_id),
                "request",
                "openai",
                "openai:gpt-5.5",
                "openai",
                "gpt-5.5",
                3,
                5,
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO model_calls(
                 id,run_id,step_index,provider_id,model_id,adapter_kind,status,
                 request_snapshot,started_at) VALUES (?,?,?,?,?,?,'completed','{}',?)""",
            (str(model_call_id), str(run_id), 1, "openai", "openai:gpt-5.5", "openai", now),
        )
        connection.execute(
            """INSERT INTO run_messages(
                 id,run_id,ordinal,role,content,model_call_id,created_at
                 ) VALUES (?,?,1,'assistant','approval required',?,?)""",
            (str(assistant_id), str(run_id), str(model_call_id), now),
        )
        connection.execute(
            """INSERT INTO tool_calls(
                 id,run_id,assistant_message_id,call_index,provider_call_id,name,
                 arguments_json,status,created_at
                 ) VALUES (?,?,?,0,'provider-tool','run_command','{}','pending',?)""",
            (str(tool_id), str(run_id), str(assistant_id), now),
        )
        connection.execute(
            "UPDATE runs SET waiting_tool_call_id = ? WHERE id = ?",
            (str(tool_id), str(run_id)),
        )
        connection.execute(
            "INSERT INTO run_events(run_id,sequence,event_type,event_version,data,created_at) "
            "VALUES (?,1,'run.queued',1,'{}',?)",
            (str(run_id), now),
        )
    sink = RecordingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        run = next(row for table, rows in sink.batches if table == "runs" for row in rows)
        tool = next(row for table, rows in sink.batches if table == "tool_calls" for row in rows)
        events = [row for table, rows in sink.batches if table == "run_events" for row in rows]
        tool_messages = [
            row
            for table, rows in sink.batches
            if table == "run_messages"
            for row in rows
            if row["role"] == "tool"
        ]
        assert run["status"] == "interrupted"
        assert run["waiting_tool_call_id"] is None
        assert run["last_event_sequence"] == 3
        assert tool["status"] == "cancelled"
        assert tool_messages[0]["tool_call_id"] == str(tool_id)
        assert tool_messages[0]["ordinal"] == 2
        assert [(event["sequence"], event["event_type"]) for event in events[-2:]] == [
            (2, "tool.result"),
            (3, "run.interrupted"),
        ]
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_rejects_another_accounts_lock_descriptor(tmp_path: Path) -> None:
    first_id, second_id = uuid4(), uuid4()
    first_dir, first_lock = await _account(tmp_path, first_id)
    _, second_lock = await _account(tmp_path, second_id)
    sink = RecordingSink()
    try:
        with pytest.raises(RuntimeError, match="account lock"):
            await import_locked_account(first_dir, first_id, second_lock, sink)
        assert sink.batches == []
    finally:
        os.close(first_lock)
        os.close(second_lock)


@pytest.mark.anyio
async def test_rejects_account_database_hardlinked_to_global_database(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    global_backup = tmp_path / "state.db"
    os.link(account_dir / "state.db", global_backup)
    sink = RecordingSink()
    try:
        with pytest.raises(RuntimeError, match="private regular file"):
            await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert sink.batches == []
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_imports_large_but_valid_visible_message(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    state = account_dir / "state.db"
    chat_id, message_id = uuid4(), uuid4()
    now = "2026-01-01T00:00:00Z"
    with closing(sqlite3.connect(state)) as connection, connection:
        connection.execute(
            "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (str(chat_id), str(user_id), "Large", now, now),
        )
        connection.execute(
            "INSERT INTO messages(id,session_id,turn_id,ordinal,role,content,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(message_id), str(chat_id), "turn", 1, "user", "x" * 300_000, now),
        )
    sink = RecordingSink()
    try:
        await import_locked_account(account_dir, user_id, lock_fd, sink)
        imported = [row for table, rows in sink.batches if table == "messages" for row in rows]
        assert imported[0]["content"] == "x" * 300_000
    finally:
        os.close(lock_fd)


@pytest.mark.anyio
async def test_rejects_oversized_row_before_any_cloud_write(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    state = account_dir / "state.db"
    chat_id, message_id = uuid4(), uuid4()
    now = "2026-01-01T00:00:00Z"
    with closing(sqlite3.connect(state)) as connection, connection:
        connection.execute(
            "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (str(chat_id), str(user_id), "Large", now, now),
        )
        connection.execute(
            "INSERT INTO messages(id,session_id,turn_id,ordinal,role,content,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(message_id), str(chat_id), "turn", 1, "user", "x" * 7_600_000, now),
        )
    sink = RecordingSink()
    try:
        with pytest.raises(RuntimeError, match="exceeds the import size limit"):
            await import_locked_account(account_dir, user_id, lock_fd, sink)
        assert sink.batches == []
        assert not (account_dir / ".supabase-imported.json").exists()
    finally:
        os.close(lock_fd)
