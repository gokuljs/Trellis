"""Bounded local commands for explicitly approved requests."""

import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import cast

from app.application.tools import ToolExecutionError, parse_command
from app.infrastructure.local_tools import _open_directory, _parts, _root

MAX_COMMAND_OUTPUT_BYTES = 20_000
MAX_COMMAND_SECONDS = 120
_EXEC_FROM_DIRECTORY = (
    "import os,sys; "
    "fd=int(sys.argv[1]); "
    "os.fchdir(fd); os.close(fd); "
    "os.execvpe(sys.argv[2], sys.argv[2:], os.environ)"
)


def _environment(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "NO_COLOR": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PIP_NO_INPUT": "1",
        "UV_NO_PROGRESS": "1",
    }


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except PermissionError:
            process.kill()
    await process.wait()


async def _capture_output(stream: asyncio.StreamReader, maximum: int) -> bytes:
    output = bytearray()
    while chunk := await stream.read(4_096):
        if len(output) + len(chunk) > maximum:
            raise ToolExecutionError("command_output_limit", "Command output exceeded its limit.")
        output.extend(chunk)
    return bytes(output)


async def run_bounded_command(
    workspace_root: Path,
    command: str,
    cwd: str = ".",
    *,
    timeout_seconds: float = MAX_COMMAND_SECONDS,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> tuple[str, bool]:
    """Start one program from the opened workspace directory descriptor."""
    arguments = parse_command(command)
    root = await asyncio.to_thread(_root, workspace_root)
    parts = _parts(cwd, allow_root=True)
    directory_fd = await asyncio.to_thread(_open_directory, root, parts)
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                _EXEC_FROM_DIRECTORY,
                str(directory_fd),
                *arguments,
                pass_fds=(directory_fd,),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=_environment(root),
                start_new_session=True,
            )
        except OSError:
            raise ToolExecutionError(
                "command_unavailable", "This command could not be started."
            ) from None
    finally:
        os.close(directory_fd)
    assert process.stdout is not None
    output_task = asyncio.create_task(_capture_output(process.stdout, max_output_bytes))
    wait_task = asyncio.create_task(process.wait())
    try:
        async with asyncio.timeout(timeout_seconds):
            output, exit_code = await asyncio.gather(output_task, wait_task)
    except ToolExecutionError:
        await _stop_process(process)
        await asyncio.gather(output_task, wait_task, return_exceptions=True)
        raise
    except TimeoutError:
        await _stop_process(process)
        await asyncio.gather(output_task, wait_task, return_exceptions=True)
        raise ToolExecutionError("command_timeout", "The command reached its time limit.") from None
    except asyncio.CancelledError:
        await _stop_process(process)
        await asyncio.gather(output_task, wait_task, return_exceptions=True)
        raise
    content = output.decode("utf-8", errors="replace").rstrip()
    if exit_code:
        raise ToolExecutionError(
            "command_failed", f"Command exited with status {exit_code}.\n{content}"
        )
    return content, False


class LocalCommandToolExecutor:
    async def execute(
        self, name: str, arguments: dict[str, object], workspace_root: Path
    ) -> tuple[str, bool]:
        if name == "run_command":
            return await run_bounded_command(
                workspace_root,
                cast(str, arguments["command"]),
                cast(str, arguments["cwd"]),
                timeout_seconds=cast(int, arguments["timeout_seconds"]),
            )
        raise ToolExecutionError("unknown_tool", "This tool is not available.")
