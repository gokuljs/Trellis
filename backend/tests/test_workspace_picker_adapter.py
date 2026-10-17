import asyncio
import json
from pathlib import Path

import pytest

from app.infrastructure.workspace_picker import NativeFolderPicker


class FakeProcess:
    def __init__(self, stdout: bytes = b"null", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode: int | None = returncode
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode or 0


def install_process(
    monkeypatch: pytest.MonkeyPatch,
    process: FakeProcess,
) -> list[tuple[object, ...]]:
    commands: list[tuple[object, ...]] = []

    async def create_subprocess_exec(*command: object, **_kwargs: object) -> FakeProcess:
        commands.append(command)
        return process

    monkeypatch.setattr(
        "app.infrastructure.workspace_picker.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )
    return commands


def test_native_folder_picker_returns_the_dialog_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=json.dumps("/tmp/project with spaces").encode())
    commands = install_process(monkeypatch, process)

    selected = asyncio.run(NativeFolderPicker().pick_directory())

    assert selected == "/tmp/project with spaces"
    assert Path(str(commands[0][-1])).name == "folder_picker_dialog.py"


def test_native_folder_picker_preserves_cancel_as_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_process(monkeypatch, FakeProcess(stdout=b"null"))

    selected = asyncio.run(NativeFolderPicker().pick_directory())

    assert selected is None


def test_native_folder_picker_rejects_a_failed_or_malformed_dialog_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=b"not-json", returncode=1)
    install_process(monkeypatch, process)

    with pytest.raises(OSError, match="could not start"):
        asyncio.run(NativeFolderPicker().pick_directory())


def test_native_folder_picker_rejects_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_process(monkeypatch, FakeProcess(stdout=b"not-json"))

    with pytest.raises(OSError, match="invalid response"):
        asyncio.run(NativeFolderPicker().pick_directory())


def test_native_folder_picker_surfaces_process_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_to_start(*_command: object, **_kwargs: object) -> FakeProcess:
        raise OSError("process launcher unavailable")

    monkeypatch.setattr(
        "app.infrastructure.workspace_picker.asyncio.create_subprocess_exec",
        fail_to_start,
    )

    with pytest.raises(OSError, match="process launcher unavailable"):
        asyncio.run(NativeFolderPicker().pick_directory())


def test_native_folder_picker_kills_the_dialog_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WaitingProcess(FakeProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Future()
            return b"null", b""

    process = WaitingProcess()
    process.returncode = None
    install_process(monkeypatch, process)

    async def cancel_picker() -> None:
        task = asyncio.create_task(NativeFolderPicker().pick_directory())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_picker())

    assert process.killed
    assert process.waited


def test_native_folder_picker_keeps_cancellation_when_the_child_exits_during_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitingProcess(FakeProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Future()
            return b"null", b""

        def kill(self) -> None:
            raise ProcessLookupError

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return -9

    process = ExitingProcess()
    process.returncode = None
    install_process(monkeypatch, process)

    async def cancel_picker() -> None:
        task = asyncio.create_task(NativeFolderPicker().pick_directory())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_picker())

    assert process.waited
