import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from run_fixtures import run_record
from test_tool_dispatch import tool_record

from app.application.errors import ApplicationError
from app.domain.runtime import ToolCallStatus, ToolResult
from app.infrastructure.local_patch import LocalPatchToolExecutor
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from app.infrastructure.workspace_execution import (
    CommandMarker,
    CommandMarkerStore,
    LocalWorkspaceExecution,
)
from app.infrastructure.workspace_grants import InstallationIdentity, WorkspaceGrantStore


def execution(tmp_path, *, installation=None, root=None, account=None, session=None):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700, exist_ok=True)
    installation = installation or InstallationIdentity.load(directory, computer_id="a" * 64)
    account = account or str(uuid4())
    account_dir = directory / account
    account_dir.mkdir(mode=0o700, exist_ok=True)
    root = root or tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    session = session or run_record().session_id
    grants = WorkspaceGrantStore(account_dir, account, installation)
    grants.select(session, root)
    owner = LocalWorkspaceExecution(
        LocalWorkspaceBindings(grants), CommandMarkerStore(directory, installation), account
    )
    return owner, root, installation


def command():
    return tool_record(
        "run_command", {"command": "echo done", "cwd": ".", "timeout_seconds": 120}, approved=True
    )


def test_dispatched_command_marker_survives_process_death_and_blocks_another_account(tmp_path):
    owner, root, installation = execution(tmp_path)
    run, tool = run_record(), command()
    metadata = root.stat()
    marker = CommandMarker(
        owner.account_id,
        run.session_id,
        run.id,
        tool.id,
        str(uuid4()),
        1,
        metadata.st_dev,
        metadata.st_ino,
    )
    script = """
import json, os, sys
from dataclasses import asdict
from pathlib import Path
from app.infrastructure.workspace_execution import CommandMarker, CommandMarkerStore
from app.infrastructure.workspace_grants import InstallationIdentity
store=CommandMarkerStore(Path(sys.argv[1]), InstallationIdentity(sys.argv[2], sys.argv[3]))
store.prepare(CommandMarker.parse(json.loads(sys.argv[4])))
os._exit(0)
"""
    from dataclasses import asdict

    subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "private"),
            installation.installation_id,
            installation.computer_id,
            json.dumps(asdict(marker)),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )

    async def exercise():
        other, _, _ = execution(
            tmp_path, installation=installation, root=root, session=str(uuid4())
        )
        other_run = replace(run, session_id=str(uuid4()))
        await other.bindings.bind(other_run.session_id, root)
        patch = tool_record("apply_patch", {"path": "a", "old_text": "", "new_text": "yes"})
        with pytest.raises(ApplicationError, match="unknown outcome"):
            async with other.operation(other_run, patch, root):
                pytest.fail("An unresolved command must block writes from another account")
        with pytest.raises(ApplicationError):
            await other.acknowledge_review(other_run.session_id, root, tool.id)
        assert owner.markers.get(metadata.st_dev, metadata.st_ino) == marker
        await owner.acknowledge_review(run.session_id, root, tool.id)
        async with other.operation(other_run, patch, root):
            pass

    asyncio.run(exercise())


def test_command_marker_is_kept_until_result_ack_and_saved_receipt_can_clear_it(tmp_path):
    owner, root, _ = execution(tmp_path)
    run, tool = run_record(), command()

    async def exercise():
        async with owner.operation(run, tool, root) as operation:
            await operation.capture_inputs()
            dispatch = await operation.mark_dispatch(str(uuid4()))
            result_snapshot = await operation.capture_result(
                ToolResult("tool", "run_command", "ok")
            )
            assert owner.markers.get(root.stat().st_dev, root.stat().st_ino) is not None
            # A lost database acknowledgment exits without confirm_saved.
        assert owner.markers.get(root.stat().st_dev, root.stat().st_ino) is not None
        saved = replace(
            tool,
            status=ToolCallStatus.COMPLETED,
            dispatch_id=dispatch,
            result_snapshot=result_snapshot,
        )
        fresh, _, _ = execution(tmp_path, root=root, account=owner.account_id)
        await fresh.validate_observations(run, [saved], root)
        assert owner.markers.get(root.stat().st_dev, root.stat().st_ino) is None

    asyncio.run(exercise())


def test_known_command_result_ack_clears_only_its_matching_marker(tmp_path):
    owner, root, _ = execution(tmp_path)
    run, tool = run_record(), command()

    async def exercise():
        async with owner.operation(run, tool, root) as operation:
            await operation.mark_dispatch(str(uuid4()))
            await operation.confirm_saved()
        assert owner.markers.get(root.stat().st_dev, root.stat().st_ino) is None

    asyncio.run(exercise())


def test_recovery_rejects_changed_read_inputs_but_accepts_saved_own_patch(tmp_path):
    owner, root, _ = execution(tmp_path)
    run = run_record()
    (root / "a.txt").write_text("before\n")
    read = tool_record("read_file", {"path": "a.txt"})
    patch = replace(
        tool_record("apply_patch", {"path": "a.txt", "old_text": "before", "new_text": "after"}),
        id=str(uuid4()),
    )

    async def exercise():
        async with owner.operation(run, read, root) as operation:
            await operation.capture_inputs()
            await operation.mark_dispatch(str(uuid4()))
            snapshot = await operation.capture_result(ToolResult("tool", "read_file", "1: before"))
        saved_read = replace(read, status=ToolCallStatus.COMPLETED, result_snapshot=snapshot)
        await owner.validate_observations(run, [saved_read], root)
        executor = LocalPatchToolExecutor()
        preview = await executor.approval_preview(patch.arguments, root)
        approved_patch = replace(patch, approval_preview=preview)
        async with owner.operation(run, approved_patch, root, [saved_read]) as operation:
            await operation.capture_inputs()
            await operation.mark_dispatch(str(uuid4()))
            result, truncated = await executor.execute(patch.arguments, root, preview)
            patch_snapshot = await operation.capture_result(
                ToolResult("tool", "apply_patch", result, truncated=truncated)
            )
        saved_patch = replace(
            patch, status=ToolCallStatus.COMPLETED, result_snapshot=patch_snapshot
        )
        await owner.validate_observations(run, [saved_read, saved_patch], root)
        (root / "a.txt").write_text("changed by another program\n")
        with pytest.raises(ApplicationError, match="changed since"):
            await owner.validate_observations(run, [saved_read, saved_patch], root)

    asyncio.run(exercise())


@pytest.mark.parametrize("damage", ["corrupt", "readable", "symlink"])
def test_unsafe_command_marker_storage_never_allows_writes(tmp_path, damage):
    owner, root, _ = execution(tmp_path)
    saved = tmp_path / "private" / "workspace-operations.json"
    if damage == "corrupt":
        saved.write_text("{bad")
        saved.chmod(0o600)
    elif damage == "readable":
        saved.write_text("{}")
        saved.chmod(0o644)
    else:
        target = tmp_path / "public"
        target.write_text("{}")
        saved.symlink_to(target)

    async def exercise():
        with pytest.raises(ApplicationError, match="permission is unavailable"):
            async with owner.operation(run_record(), command(), root):
                pytest.fail("Unsafe marker storage must fail closed")

    asyncio.run(exercise())


@pytest.mark.parametrize("lost_ack", ["dispatch", "result", "none"])
def test_runtime_command_marker_precedes_dispatch_and_survives_lost_acknowledgments(
    tmp_path, lost_ack, monkeypatch
):
    from test_tool_dispatch import ProbeExecutor, ToolRepository, runtime_for

    async def exercise():
        owner, root, _ = execution(tmp_path)
        tool = replace(command(), approval_preview=command().arguments)
        repository = ToolRepository(tool)
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor, command=True)
        runtime._workspace_execution = owner
        begin = repository.begin_tool_execution
        save = repository.record_tool_result

        async def begin_with_marker(*args, **kwargs):
            marker = owner.markers.get(root.stat().st_dev, root.stat().st_ino)
            assert marker is not None
            assert marker.dispatch_id == kwargs["dispatch_id"]
            return await begin(*args, **kwargs)

        async def save_metadata(*args, result_snapshot=None, **kwargs):
            outcome = await save(*args, **kwargs)
            repository.tool = replace(repository.tool, result_snapshot=result_snapshot)
            if lost_ack == "result":
                raise ApplicationError("database_unavailable", "Result acknowledgment lost.")
            return outcome

        monkeypatch.setattr(repository, "begin_tool_execution", begin_with_marker)
        monkeypatch.setattr(repository, "record_tool_result", save_metadata)
        repository.lose_dispatch_reply = lost_ack == "dispatch"
        if lost_ack == "none":
            assert await runtime._execute_pending_tools(repository.run, root)
            assert owner.markers.get(root.stat().st_dev, root.stat().st_ino) is None
        else:
            with pytest.raises(ApplicationError):
                await runtime._execute_pending_tools(repository.run, root)
            assert owner.markers.get(root.stat().st_dev, root.stat().st_ino) is not None
            if lost_ack == "result":
                await owner.validate_observations(repository.run, [repository.tool], root)
                assert owner.markers.get(root.stat().st_dev, root.stat().st_ino) is None
        assert executor.effects == (0 if lost_ack == "dispatch" else 1)

    asyncio.run(exercise())


def test_cancelled_write_retains_directory_lock_until_actual_thread_finishes(tmp_path):
    import fcntl
    import threading

    from app.infrastructure.local_tools import _run_bounded_file_operation

    owner, root, _ = execution(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def blocked_write(_root, _arguments, _stopped):
        entered.set()
        assert release.wait(3)
        return "done"

    async def exercise():
        async def write():
            async with owner.guard(run_record().session_id, root, exclusive=True):
                await _run_bounded_file_operation(blocked_write, root, {})

        task = asyncio.create_task(write())
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            release.set()
            os.close(descriptor)

    asyncio.run(exercise())


def test_completed_patch_without_previous_read_tracks_its_target(tmp_path):
    owner, root, _ = execution(tmp_path)
    run = run_record()
    (root / "target.txt").write_text("before")
    arguments = {"path": "target.txt", "old_text": "before", "new_text": "after"}
    executor = LocalPatchToolExecutor()

    async def exercise():
        preview = await executor.approval_preview(arguments, root)
        tool = replace(
            tool_record("apply_patch", arguments, approved=True), approval_preview=preview
        )
        async with owner.operation(run, tool, root) as operation:
            await operation.capture_inputs()
            await operation.mark_dispatch(str(uuid4()))
            content, _ = await executor.execute(arguments, root, preview)
            snapshot = await operation.capture_result(ToolResult("tool", "apply_patch", content))
        saved = replace(tool, status=ToolCallStatus.COMPLETED, result_snapshot=snapshot)
        await owner.validate_observations(run, [saved], root)
        (root / "target.txt").write_text("external change")
        with pytest.raises(ApplicationError, match="changed since"):
            await owner.validate_observations(run, [saved], root)

    asyncio.run(exercise())


@pytest.mark.parametrize("ending", ["cancelled", "interrupted", "failed"])
def test_cleanup_preserves_unknown_patch_after_retry_authorization(ending):
    from test_tool_dispatch import ProbeExecutor, ToolRepository, runtime_for

    async def exercise():
        tool = replace(
            tool_record("apply_patch", {"path": "a", "old_text": "before", "new_text": "after"}),
            status=ToolCallStatus.PENDING,
            dispatch_attempt=1,
            dispatch_generation=0,
            recovery_policy="patch",
        )
        repository = ToolRepository(tool)
        runtime = runtime_for(repository, ProbeExecutor(repository))
        if ending == "cancelled":
            await runtime._mark_cancelled(repository.run.id, None)
        elif ending == "interrupted":
            await runtime._mark_interrupted(repository.run.id, None)
        else:
            await runtime._fail(repository.run.id, None, "failure", "Stopped.")
        assert repository.tool.status is ToolCallStatus.OUTCOME_UNKNOWN
        assert not repository.results
        assert not repository.run.retry_allowed

    asyncio.run(exercise())
