import asyncio
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from app.application.tools import ToolExecutionError, ToolRegistry
from app.domain.models import TestPreset as SavedTestPreset
from app.domain.runtime import ModelToolCall
from app.infrastructure.command_tools import LocalCommandToolExecutor, run_bounded_command
from app.infrastructure.database import Database
from app.infrastructure.local_tools import LocalReadToolExecutor


def _python(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def test_command_runs_in_validated_workspace_directory_with_bounded_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    command = _python("import os; print(os.getcwd()); print(os.getenv('OPENAI_API_KEY', 'absent'))")

    content, truncated = asyncio.run(run_bounded_command(tmp_path, command, "src"))

    assert content.splitlines() == [str(tmp_path / "src"), "absent"]
    assert not truncated


@pytest.mark.parametrize("cwd", ["../outside", "/tmp", "link"])
def test_command_rejects_cwd_escape(tmp_path: Path, cwd: str) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(run_bounded_command(tmp_path, _python("print('ran')"), cwd))

    assert error.value.code == "path_not_allowed"


def test_command_rejects_shell_operators_without_running_them(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    command = f"{_python('print(1)')} ; touch {shlex.quote(str(marker))}"

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(run_bounded_command(tmp_path, command, "."))

    assert error.value.code == "invalid_command"
    assert not marker.exists()


def test_command_uses_validated_directory_when_path_is_swapped_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inside = tmp_path / "inside"
    original = tmp_path / "original"
    outside = tmp_path.parent / "outside-cwd"
    inside.mkdir()
    outside.mkdir(exist_ok=True)
    create_process = asyncio.create_subprocess_exec

    async def swap_before_spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        inside.rename(original)
        inside.symlink_to(outside, target_is_directory=True)
        return await create_process(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", swap_before_spawn)
    command = _python("open('ran-here', 'w').write('yes')")

    asyncio.run(run_bounded_command(tmp_path, command, "inside"))

    assert (original / "ran-here").read_text() == "yes"
    assert not (outside / "ran-here").exists()


def test_command_stops_at_output_limit(tmp_path: Path) -> None:
    command = _python("print('x' * 30000)")

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(run_bounded_command(tmp_path, command, ".", max_output_bytes=1024))

    assert error.value.code == "command_output_limit"


def test_timeout_stops_child_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child-finished"
    child = _python(f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('bad')")
    parent = _python(
        f"import subprocess, time; subprocess.Popen({shlex.split(child)!r}); time.sleep(2)"
    )

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(run_bounded_command(tmp_path, parent, ".", timeout_seconds=0.2))
    assert error.value.code == "command_timeout"
    asyncio.run(asyncio.sleep(1.1))
    assert not marker.exists()


def test_cancellation_stops_child_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child-finished"
    child = _python(f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('bad')")
    parent = _python(
        f"import subprocess, time; subprocess.Popen({shlex.split(child)!r}); time.sleep(2)"
    )

    async def cancel() -> None:
        task = asyncio.create_task(run_bounded_command(tmp_path, parent, "."))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    asyncio.run(asyncio.sleep(1.1))
    assert not marker.exists()


def test_run_command_requires_approval_and_revalidates_preview(tmp_path: Path) -> None:
    database = Database(tmp_path / "data.db")
    asyncio.run(database.initialize())
    registry = ToolRegistry(
        LocalReadToolExecutor(), command_executor=LocalCommandToolExecutor(database)
    )
    call = ModelToolCall(
        "call-1",
        "run_command",
        {"command": _python("print('approved')"), "cwd": "."},
    )

    assert registry.requires_approval(call, tmp_path)
    assert "run_command" in {spec.name for spec in registry.specs()}
    blocked = asyncio.run(registry.execute(call, tmp_path))
    preview = asyncio.run(registry.approval_preview(call, tmp_path))
    altered = asyncio.run(
        registry.execute(call, tmp_path, approved=True, approval_preview={"command": "other"})
    )
    approved = asyncio.run(
        registry.execute(call, tmp_path, approved=True, approval_preview=preview)
    )

    assert blocked.error_code == "approval_required"
    assert preview is not None and preview["command"] == call.arguments["command"]
    assert altered.error_code == "approval_changed"
    assert approved.content == "approved"
    assert not approved.is_error


def test_run_test_can_only_execute_a_saved_exact_workspace_preset(tmp_path: Path) -> None:
    database = Database(tmp_path / "data.db")
    asyncio.run(database.initialize())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    asyncio.run(
        database.save_test_preset(
            str(workspace), SavedTestPreset("Backend", _python("print('saved')"), ".")
        )
    )
    registry = ToolRegistry(
        LocalReadToolExecutor(), command_executor=LocalCommandToolExecutor(database)
    )
    listed = asyncio.run(
        registry.execute(ModelToolCall("list", "list_test_presets", {}), workspace)
    )
    unsaved = asyncio.run(
        registry.execute(ModelToolCall("unknown", "run_test", {"name": "Injected"}), workspace)
    )
    saved = asyncio.run(
        registry.execute(ModelToolCall("saved", "run_test", {"name": "Backend"}), workspace)
    )

    assert "Backend" in listed.content
    assert "saved" not in listed.content
    assert unsaved.error_code == "unknown_test_preset"
    assert saved.content == "saved"
    assert not saved.is_error
