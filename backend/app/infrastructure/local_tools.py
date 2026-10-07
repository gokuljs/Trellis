"""Bounded local file and Git inspection for agent runs."""

import asyncio
import os
import signal
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import cast

from app.application.context import MAX_WORKSPACE_GUIDANCE_BYTES
from app.application.tools import ToolExecutionError

MAX_OUTPUT_BYTES = 20_000
MAX_FILE_BYTES = 1_000_000
MAX_SEARCH_FILE_BYTES = 256_000
MAX_SEARCH_TOTAL_BYTES = 4_000_000
MAX_SCAN_ENTRIES = 5_000
MAX_WALK_DEPTH = 16
TOOL_TIMEOUT_SECONDS = 5

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SKIP_DIRECTORIES = frozenset({".venv", "node_modules", "dist", "build", ".cache"})
_PROTECTED_NAMES = frozenset(
    {
        ".git",
        ".env",
        ".envrc",
        ".aws",
        ".ssh",
        ".trellis",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        ".pgpass",
        ".my.cnf",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "secrets.json",
    }
)


def _protected(name: str) -> bool:
    lower = name.casefold()
    return (
        lower in _PROTECTED_NAMES or lower.startswith(".env.") or lower.endswith((".pem", ".key"))
    )


def _parts(relative_path: str, *, allow_root: bool) -> tuple[str, ...]:
    if (
        not relative_path
        or "\x00" in relative_path
        or "\\" in relative_path
        or Path(relative_path).is_absolute()
        or PureWindowsPath(relative_path).is_absolute()
    ):
        raise ToolExecutionError("path_not_allowed", "This workspace path is not allowed.")
    parts = Path(relative_path).parts
    if any(part == ".." or _protected(part) for part in parts):
        raise ToolExecutionError("path_not_allowed", "This workspace path is not allowed.")
    if not parts and not allow_root:
        raise ToolExecutionError("path_not_allowed", "This workspace path is not allowed.")
    return parts


def _root(workspace_root: Path) -> Path:
    try:
        if (
            not workspace_root.is_absolute()
            or workspace_root.resolve(strict=True) != workspace_root
            or not workspace_root.is_dir()
        ):
            raise ValueError
    except OSError, ValueError:
        raise ToolExecutionError("invalid_workspace", "The workspace is unavailable.") from None
    return workspace_root


def _open_directory(root: Path, parts: tuple[str, ...]) -> int:
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
        for part in parts:
            try:
                next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            finally:
                os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError:
        raise ToolExecutionError(
            "path_not_allowed", "This workspace path is not allowed."
        ) from None


def _read_bytes(directory_fd: int, name: str, limit: int) -> bytes:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ToolExecutionError("not_text_file", "This path is not a text file.")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                return source.read(limit + 1)
        finally:
            os.close(descriptor)
    except OSError:
        raise ToolExecutionError(
            "path_not_allowed", "This workspace path is not allowed."
        ) from None


@dataclass(slots=True)
class _WalkState:
    visited: int = 0
    truncated: bool = False


def _walk_files(
    directory_fd: int,
    prefix: str,
    depth: int,
    state: _WalkState,
    stopped: threading.Event,
) -> Iterator[tuple[str, int, str]]:
    if depth > MAX_WALK_DEPTH:
        state.truncated = True
        return
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                if stopped.is_set() or state.visited >= MAX_SCAN_ENTRIES:
                    state.truncated = True
                    return
                state.visited += 1
                names.append(entry.name)
    except OSError:
        state.truncated = True
        return
    for name in sorted(names):
        if stopped.is_set():
            state.truncated = True
            return
        if _protected(name):
            continue
        try:
            mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
        except OSError:
            state.truncated = True
            continue
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISDIR(mode):
            if name in _SKIP_DIRECTORIES:
                continue
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            except OSError:
                state.truncated = True
                continue
            try:
                yield from _walk_files(child_fd, relative, depth + 1, state, stopped)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(mode):
            yield relative, directory_fd, name


def _format_lines(lines: list[str]) -> tuple[str, bool]:
    output: list[str] = []
    size = 0
    for line in lines:
        line_size = len(line.encode("utf-8")) + (1 if output else 0)
        if size + line_size > MAX_OUTPUT_BYTES:
            remaining = MAX_OUTPUT_BYTES - size - (1 if output else 0)
            if remaining > 0:
                output.append(line.encode("utf-8")[:remaining].decode("utf-8", errors="ignore"))
            return "\n".join(output), True
        output.append(line)
        size += line_size
    return "\n".join(output), False


def _list_files(
    root: Path, arguments: dict[str, object], stopped: threading.Event
) -> tuple[str, bool]:
    parts = _parts(cast(str, arguments["path"]), allow_root=True)
    limit = cast(int, arguments["limit"])
    directory_fd = _open_directory(root, parts)
    state = _WalkState()
    paths: list[str] = []
    try:
        for relative, _directory_fd, _name in _walk_files(
            directory_fd, "/".join(parts), 0, state, stopped
        ):
            if len(paths) >= limit:
                state.truncated = True
                break
            paths.append(relative)
    finally:
        os.close(directory_fd)
    content, output_truncated = _format_lines(paths)
    return content, state.truncated or output_truncated


def _read_file(
    root: Path, arguments: dict[str, object], _stopped: threading.Event
) -> tuple[str, bool]:
    parts = _parts(cast(str, arguments["path"]), allow_root=False)
    parent_fd = _open_directory(root, parts[:-1])
    try:
        data = _read_bytes(parent_fd, parts[-1], MAX_FILE_BYTES)
    finally:
        os.close(parent_fd)
    if len(data) > MAX_FILE_BYTES:
        raise ToolExecutionError("file_too_large", "This file exceeds the read limit.")
    if b"\x00" in data:
        raise ToolExecutionError("not_text_file", "This path is not a text file.")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ToolExecutionError("not_text_file", "This path is not a text file.") from None
    start = cast(int, arguments["start_line"]) - 1
    count = cast(int, arguments["max_lines"])
    selected = [
        f"{index + 1}: {line}" for index, line in enumerate(lines) if start <= index < start + count
    ]
    content, output_truncated = _format_lines(selected)
    return content, output_truncated or start + count < len(lines)


def _search_files(
    root: Path, arguments: dict[str, object], stopped: threading.Event
) -> tuple[str, bool]:
    parts = _parts(cast(str, arguments["path"]), allow_root=True)
    query = cast(str, arguments["query"])
    limit = cast(int, arguments["limit"])
    case_sensitive = cast(bool, arguments["case_sensitive"])
    needle = query if case_sensitive else query.casefold()
    directory_fd = _open_directory(root, parts)
    state = _WalkState()
    total_bytes = 0
    matches: list[str] = []
    try:
        for relative, parent_fd, name in _walk_files(
            directory_fd, "/".join(parts), 0, state, stopped
        ):
            try:
                data = _read_bytes(parent_fd, name, MAX_SEARCH_FILE_BYTES)
            except ToolExecutionError:
                state.truncated = True
                continue
            total_bytes += len(data)
            if total_bytes > MAX_SEARCH_TOTAL_BYTES:
                state.truncated = True
                break
            if len(data) > MAX_SEARCH_FILE_BYTES:
                state.truncated = True
                continue
            if b"\x00" in data:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                match_at = haystack.find(needle)
                if match_at < 0:
                    continue
                if len(matches) >= limit:
                    state.truncated = True
                    break
                snippet_start = max(0, match_at - 80)
                snippet_end = min(len(line), match_at + len(query) + 80)
                snippet = line[snippet_start:snippet_end]
                if snippet_start:
                    snippet = f"…{snippet}"
                    state.truncated = True
                if snippet_end < len(line):
                    snippet = f"{snippet}…"
                    state.truncated = True
                matches.append(f"{relative}:{number}:{snippet}")
            if len(matches) >= limit and state.truncated:
                break
    finally:
        os.close(directory_fd)
    content, output_truncated = _format_lines(matches)
    return content, state.truncated or output_truncated


async def _run_bounded_file_operation[T](
    operation: Callable[[Path, dict[str, object], threading.Event], T],
    root: Path,
    arguments: dict[str, object],
) -> T:
    stopped = threading.Event()
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            return await asyncio.to_thread(operation, root, arguments, stopped)
    except TimeoutError:
        stopped.set()
        raise ToolExecutionError("tool_timeout", "The tool reached its time limit.") from None
    except asyncio.CancelledError:
        stopped.set()
        raise


class _OutputLimit(Exception):
    def __init__(self, prefix: bytes, stream_name: str) -> None:
        self.prefix = prefix
        self.stream_name = stream_name


async def _read_stream(stream: asyncio.StreamReader, maximum: int, stream_name: str) -> bytes:
    output = bytearray()
    while chunk := await stream.read(4096):
        output.extend(chunk)
        if len(output) > maximum:
            raise _OutputLimit(bytes(output[:maximum]), stream_name)
    return bytes(output)


async def _stop_process(
    process: asyncio.subprocess.Process,
    tasks: tuple[asyncio.Task[bytes] | asyncio.Task[int], ...],
) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except PermissionError, ProcessLookupError:
        # Some local sandboxes permit terminating a direct child but not its process group.
        with suppress(ProcessLookupError):
            process.kill()
    await process.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _git_command(
    root: Path, *arguments: str, limit: int = MAX_OUTPUT_BYTES
) -> tuple[bytes, bool]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "--literal-pathspecs",
            "--no-optional-locks",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
    except OSError:
        raise ToolExecutionError("git_unavailable", "Git inspection is unavailable.") from None
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_read_stream(process.stdout, limit, "stdout"))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, 4096, "stderr"))
    wait_task = asyncio.create_task(process.wait())
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            stdout, _stderr, exit_code = await asyncio.gather(stdout_task, stderr_task, wait_task)
    except _OutputLimit as error:
        await _stop_process(process, (stdout_task, stderr_task, wait_task))
        if error.stream_name == "stderr":
            raise ToolExecutionError(
                "git_output_limit", "Git diagnostics exceeded the read limit."
            ) from None
        return error.prefix, True
    except TimeoutError, asyncio.CancelledError:
        await _stop_process(process, (stdout_task, stderr_task, wait_task))
        raise
    if exit_code:
        raise ToolExecutionError("git_failed", "Git inspection could not complete.")
    return stdout, False


async def _inspect_git(root: Path, operation: str) -> tuple[str, bool]:
    try:
        top_level, _ = await _git_command(root, "rev-parse", "--show-toplevel")
    except ToolExecutionError as error:
        if error.code == "git_failed":
            raise ToolExecutionError(
                "invalid_workspace", "The workspace is not a Git root."
            ) from None
        raise
    resolved_git_root = await asyncio.to_thread(Path(os.fsdecode(top_level.strip())).resolve)
    if resolved_git_root != root:
        raise ToolExecutionError("invalid_workspace", "The workspace is not a Git root.")

    if operation == "status":
        raw_status, truncated = await _git_command(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=normal", limit=64_000
        )
        entries = raw_status.split(b"\x00")
        status_lines: list[str] = []
        index = 0
        while index < len(entries) - 1:
            entry = entries[index]
            index += 1
            if len(entry) < 4 or entry[2:3] != b" ":
                continue
            path = os.fsdecode(entry[3:])
            other_path: str | None = None
            if (entry[0:1] in (b"R", b"C") or entry[1:2] in (b"R", b"C")) and index < len(
                entries
            ) - 1:
                other_path = os.fsdecode(entries[index])
                index += 1
            if any(_protected(part) for part in Path(path).parts) or (
                other_path is not None and any(_protected(part) for part in Path(other_path).parts)
            ):
                continue
            display_path = path.replace("\n", "\\n").replace("\r", "\\r")
            status_lines.append(f"{entry[:2].decode('ascii', errors='replace')} {display_path}")
        content, output_truncated = _format_lines(status_lines)
        return content, truncated or output_truncated
    elif operation == "log":
        output, truncated = await _git_command(root, "log", "-n", "10", "--oneline")
    else:
        names, names_truncated = await _git_command(
            root, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", limit=64_000
        )
        if names_truncated:
            raise ToolExecutionError("git_output_limit", "The Git diff exceeds the read limit.")
        safe_paths = [
            path
            for path in (os.fsdecode(item) for item in names.split(b"\x00") if item)
            if not any(_protected(part) for part in Path(path).parts)
        ]
        if not safe_paths:
            return "", False
        if len(safe_paths) > 200:
            raise ToolExecutionError("git_output_limit", "The Git diff exceeds the read limit.")
        output, truncated = await _git_command(
            root, "diff", "--no-ext-diff", "--no-textconv", "--", *safe_paths
        )
    return output.decode("utf-8", errors="replace").rstrip(), truncated


class LocalReadToolExecutor:
    async def execute(
        self, name: str, arguments: dict[str, object], workspace_root: Path
    ) -> tuple[str, bool]:
        root = await asyncio.to_thread(_root, workspace_root)
        if name == "list_files":
            return await _run_bounded_file_operation(_list_files, root, arguments)
        if name == "search_files":
            return await _run_bounded_file_operation(_search_files, root, arguments)
        if name == "read_file":
            return await _run_bounded_file_operation(_read_file, root, arguments)
        if name == "inspect_git":
            try:
                return await _inspect_git(root, cast(str, arguments["operation"]))
            except TimeoutError:
                raise ToolExecutionError(
                    "tool_timeout", "The tool reached its time limit."
                ) from None
        raise ToolExecutionError("unknown_tool", "This tool is not available.")


def _guidance_file_operation(
    root: Path, _arguments: dict[str, object], stopped: threading.Event
) -> str | None:
    if stopped.is_set():
        return None
    directory_fd = _open_directory(root, ())
    try:
        try:
            data = _read_bytes(directory_fd, "AGENTS.md", MAX_WORKSPACE_GUIDANCE_BYTES)
        except ToolExecutionError as error:
            if error.code in {"path_not_allowed", "not_text_file"}:
                return None
            raise
    finally:
        os.close(directory_fd)
    if len(data) > MAX_WORKSPACE_GUIDANCE_BYTES or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def read_workspace_guidance(workspace_root: Path) -> str | None:
    """Read a bounded root AGENTS.md without following a workspace symlink."""
    root = await asyncio.to_thread(_root, workspace_root)
    return await _run_bounded_file_operation(_guidance_file_operation, root, {})
