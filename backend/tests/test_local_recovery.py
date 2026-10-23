import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.application.tools import ToolExecutionError
from app.infrastructure import command_tools, local_patch, local_tools
from app.infrastructure.local_patch import LocalPatchToolExecutor, reconcile_patch


def test_cancelled_file_operation_drains_thread_before_returning(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = threading.Event()
        exited = threading.Event()
        loop = asyncio.get_running_loop()

        def operation(_root, _arguments, stopped):
            loop.call_soon_threadsafe(started.set)
            release.wait(2)
            assert stopped.is_set()
            exited.set()

        task = asyncio.create_task(local_tools._run_bounded_file_operation(operation, tmp_path, {}))
        await started.wait()
        try:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "caller must retain its lock until the write thread exits"
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert exited.is_set()

    asyncio.run(scenario())


def test_patch_reconciles_original_desired_and_conflicting_contents(tmp_path: Path) -> None:
    async def scenario() -> None:
        target = tmp_path / "hello.txt"
        target.write_text("hello world\n")
        arguments = {"path": "hello.txt", "old_text": "hello", "new_text": "goodbye"}
        executor = LocalPatchToolExecutor()
        preview = await executor.approval_preview(arguments, tmp_path)
        assert await reconcile_patch(arguments, preview, tmp_path) == "unchanged_original"
        assert await executor.execute(arguments, tmp_path, preview) == ("Updated hello.txt.", False)
        before = target.stat()
        assert await reconcile_patch(arguments, preview, tmp_path) == "desired_state_present"
        assert target.stat() == before
        assert target.read_text() == "goodbye world\n"
        target.write_text("somebody else's edit\n")
        assert await reconcile_patch(arguments, preview, tmp_path) == "conflict"

    asyncio.run(scenario())


def test_created_file_reconciles_absence_and_creation(tmp_path: Path) -> None:
    async def scenario() -> None:
        arguments = {"path": "created.txt", "old_text": "", "new_text": "new file\n"}
        executor = LocalPatchToolExecutor()
        preview = await executor.approval_preview(arguments, tmp_path)
        assert preview["target_sha256"] is None
        assert await reconcile_patch(arguments, preview, tmp_path) == "unchanged_original"
        await executor.execute(arguments, tmp_path, preview)
        assert await reconcile_patch(arguments, preview, tmp_path) == "desired_state_present"

    asyncio.run(scenario())


def test_patch_reconciliation_rejects_changed_approval_inputs(tmp_path: Path) -> None:
    async def scenario() -> None:
        (tmp_path / "hello.txt").write_text("hello\n")
        guidance = tmp_path / "AGENTS.md"
        guidance.write_text("Use approved edits only.\n")
        arguments = {"path": "hello.txt", "old_text": "hello", "new_text": "goodbye"}
        preview = await LocalPatchToolExecutor().approval_preview(arguments, tmp_path)
        for replacement in (
            {**preview, "desired_sha256": "invalid"},
            {**preview, "desired_sha256": "0" * 64},
            {**preview, "target_identity": None},
            {**preview, "diff": "another diff"},
        ):
            assert await reconcile_patch(arguments, replacement, tmp_path) == "conflict"
        changed = {**arguments, "new_text": "unexpected"}
        assert await reconcile_patch(changed, preview, tmp_path) == "conflict"
        guidance.write_text("New guidance.\n")
        assert await reconcile_patch(arguments, preview, tmp_path) == "conflict"

    asyncio.run(scenario())


def test_process_death_after_replace_reconciles_without_another_write(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hello world\n")
    arguments = {"path": "hello.txt", "old_text": "hello", "new_text": "goodbye"}
    preview = asyncio.run(LocalPatchToolExecutor().approval_preview(arguments, tmp_path))
    script = """
import json, os, sys, threading
from pathlib import Path
from app.infrastructure import local_patch
replace = os.replace
def replace_and_die(*arguments, **keywords):
    replace(*arguments, **keywords)
    os._exit(73)
local_patch.os.replace = replace_and_die
local_patch._apply_operation(
    Path(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[3]), threading.Event()
)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(arguments), json.dumps(preview)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        timeout=10,
    )
    assert child.returncode == 73
    assert target.read_text() == "goodbye world\n"
    identity = target.stat()
    assert asyncio.run(reconcile_patch(arguments, preview, tmp_path)) == "desired_state_present"
    assert target.stat() == identity


def test_pinned_workspace_rejects_replaced_root(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "workspace"
        root.mkdir()
        (root / "hello.txt").write_text("trusted\n")
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with local_tools.pinned_workspace(root, descriptor, lambda: True):
                root.rename(tmp_path / "moved")
                root.mkdir()
                (root / "hello.txt").write_text("untrusted\n")
                with pytest.raises(ToolExecutionError, match="trusted workspace changed"):
                    await local_tools.LocalReadToolExecutor().execute(
                        "read_file", {"path": "hello.txt", "start_line": 1, "max_lines": 5}, root
                    )
        finally:
            os.close(descriptor)

    asyncio.run(scenario())


def test_patch_rechecks_revocation_before_final_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        target = tmp_path / "hello.txt"
        target.write_text("hello\n")
        arguments = {"path": "hello.txt", "old_text": "hello", "new_text": "goodbye"}
        executor = LocalPatchToolExecutor()
        preview = await executor.approval_preview(arguments, tmp_path)
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        trusted = True
        write_all = local_patch._write_all

        def write_then_revoke(file_descriptor, data):
            nonlocal trusted
            write_all(file_descriptor, data)
            trusted = False

        monkeypatch.setattr(local_patch, "_write_all", write_then_revoke)
        try:
            with (
                local_tools.pinned_workspace(tmp_path, descriptor, lambda: trusted),
                pytest.raises(ToolExecutionError, match="trusted workspace changed"),
            ):
                await executor.execute(arguments, tmp_path, preview)
        finally:
            os.close(descriptor)
        assert target.read_text() == "hello\n"
        assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == [target]

    asyncio.run(scenario())


def test_cancellation_during_spawn_waits_for_child_and_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        launched = asyncio.Event()
        release = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []
        create_subprocess = asyncio.create_subprocess_exec

        async def delayed_spawn(*arguments, **keywords):
            child = await create_subprocess(*arguments, **keywords)
            children.append(child)
            launched.set()
            await release.wait()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        command = shlex.join((sys.executable, "-c", "import time; time.sleep(20)"))
        task = asyncio.create_task(command_tools.run_bounded_command(tmp_path, command))
        await asyncio.wait_for(launched.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(children) == 1
        assert children[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(children[0].pid, 0)

    asyncio.run(scenario())


def test_command_timeout_terminates_child_and_keeps_bounded_error(tmp_path: Path) -> None:
    command = shlex.join((sys.executable, "-c", "import time; time.sleep(20)"))
    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(command_tools.run_bounded_command(tmp_path, command, timeout_seconds=0.05))
    assert error.value.code == "command_timeout"


def test_command_output_and_directory_remain_available(tmp_path: Path) -> None:
    command = shlex.join((sys.executable, "-c", "from pathlib import Path; print(Path.cwd())"))
    assert asyncio.run(command_tools.run_bounded_command(tmp_path, command)) == (
        str(tmp_path),
        False,
    )


def test_timed_out_file_operation_drains_thread_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        timeout_observed = asyncio.Event()
        release = threading.Event()
        exited = threading.Event()
        loop = asyncio.get_running_loop()

        def operation(_root, _arguments, stopped):
            assert stopped.wait(2)
            loop.call_soon_threadsafe(timeout_observed.set)
            release.wait(2)
            exited.set()

        monkeypatch.setattr(local_tools, "TOOL_TIMEOUT_SECONDS", 0.01)
        task = asyncio.create_task(local_tools._run_bounded_file_operation(operation, tmp_path, {}))
        await asyncio.wait_for(timeout_observed.wait(), 3)
        try:
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(ToolExecutionError) as error:
                await task
        assert error.value.code == "tool_timeout"
        assert exited.is_set()

    asyncio.run(scenario())


def test_git_inspection_never_reads_a_replaced_workspace_root(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    for path, message in ((root, "TRUSTED"), (replacement, "UNTRUSTED_REPLACEMENT")):
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@example.test",
                "commit",
                "--allow-empty",
                "-qm",
                message,
            ],
            check=True,
        )
    git_command = local_tools._git_command
    replaced = False

    async def replace_before_read(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            root.rename(tmp_path / "original")
            replacement.rename(root)
            replaced = True
        return await git_command(*args, **kwargs)

    monkeypatch.setattr(local_tools, "_git_command", replace_before_read)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)

    async def exercise():
        with (
            local_tools.pinned_workspace(root, descriptor, lambda: True),
            pytest.raises(ToolExecutionError, match="trusted workspace changed"),
        ):
            await local_tools.LocalReadToolExecutor().execute(
                "inspect_git", {"operation": "log"}, root
            )

    try:
        asyncio.run(exercise())
    finally:
        os.close(descriptor)


def test_cancellation_during_git_spawn_waits_for_child_cleanup(tmp_path, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        create = asyncio.create_subprocess_exec
        children = []

        async def delayed_spawn(*args, **kwargs):
            child = await create(*args, **kwargs)
            children.append(child)
            entered.set()
            await release.wait()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        task = asyncio.create_task(local_tools._git_command(tmp_path, "--version"))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(children) == 1
        assert children[0].returncode is not None

    asyncio.run(exercise())
