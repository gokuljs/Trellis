"""Preview and apply exact workspace text edits without following symlinks."""

import difflib
import hashlib
import json
import os
import secrets
import stat
import threading
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from app.application.context import MAX_WORKSPACE_GUIDANCE_BYTES
from app.application.tools import ToolExecutionError, redact_secrets
from app.infrastructure.local_tools import (
    MAX_FILE_BYTES,
    _open_directory,
    _parts,
    _read_bytes,
    _root,
    _run_bounded_file_operation,
)

MAX_PATCH_DIFF_BYTES = 20_000


def _arguments(arguments: dict[str, object]) -> tuple[str, str, str]:
    path = arguments.get("path")
    old_text = arguments.get("old_text")
    new_text = arguments.get("new_text")
    if (
        not isinstance(path, str)
        or not isinstance(old_text, str)
        or not isinstance(new_text, str)
        or old_text == new_text
        or (not old_text and not new_text)
        or "\x00" in old_text
        or "\x00" in new_text
    ):
        raise ToolExecutionError("invalid_tool_arguments", "Tool arguments are invalid.")
    try:
        path.encode("utf-8")
        old_text.encode("utf-8")
        new_text.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolExecutionError("invalid_tool_arguments", "Tool arguments are invalid.") from None
    return path, old_text, new_text


def _target_parts(path: str) -> tuple[str, ...]:
    parts = _parts(path, allow_root=False)
    if any(
        part.casefold() in {"agents.md", ".agents", ".codex"} or "\n" in part or "\r" in part
        for part in parts
    ):
        raise ToolExecutionError("path_not_allowed", "This workspace path is not allowed.")
    return parts


def _target_identity(parent_fd: int, name: str) -> dict[str, int] | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise ToolExecutionError(
            "path_not_allowed", "This workspace path is not allowed."
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise ToolExecutionError("path_not_allowed", "This workspace path is not allowed.")
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size,
        "modified_ns": info.st_mtime_ns,
    }


def _read_target(parent_fd: int, name: str) -> tuple[bytes, dict[str, int]] | None:
    identity = _target_identity(parent_fd, name)
    if identity is None:
        return None
    data = _read_bytes(parent_fd, name, MAX_FILE_BYTES)
    if _target_identity(parent_fd, name) != identity:
        raise ToolExecutionError("target_changed", "The file changed during the edit.")
    if len(data) > MAX_FILE_BYTES:
        raise ToolExecutionError("file_too_large", "This file exceeds the edit limit.")
    if b"\x00" in data:
        raise ToolExecutionError("not_text_file", "This path is not a text file.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolExecutionError("not_text_file", "This path is not a text file.") from None
    return data, identity


def _ancestor_guidance(
    root: Path, target_parts: tuple[str, ...]
) -> tuple[list[dict[str, str]], dict[str, str]]:
    guidance: list[dict[str, str]] = []
    hashes: dict[str, str] = {}
    remaining = MAX_WORKSPACE_GUIDANCE_BYTES
    for depth in range(len(target_parts)):
        directory_fd = _open_directory(root, target_parts[:depth])
        try:
            try:
                mode = os.stat("AGENTS.md", dir_fd=directory_fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            except OSError:
                raise ToolExecutionError(
                    "guidance_unavailable", "Workspace guidance cannot be read safely."
                ) from None
            if not stat.S_ISREG(mode):
                raise ToolExecutionError(
                    "guidance_unavailable", "Workspace guidance cannot be read safely."
                )
            data = _read_bytes(directory_fd, "AGENTS.md", remaining)
        finally:
            os.close(directory_fd)
        if len(data) > remaining or b"\x00" in data:
            raise ToolExecutionError(
                "guidance_unavailable", "Workspace guidance exceeds the limit."
            )
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolExecutionError(
                "guidance_unavailable", "Workspace guidance is not UTF-8 text."
            ) from None
        relative = "/".join((*target_parts[:depth], "AGENTS.md"))
        guidance.append({"path": relative, "content": redact_secrets(content)})
        hashes[relative] = hashlib.sha256(data).hexdigest()
        remaining -= len(data)
    return guidance, hashes


def _replace_once(content: str, old_text: str, new_text: str) -> str:
    first = content.find(old_text)
    if first < 0:
        raise ToolExecutionError("target_changed", "The original text is no longer present.")
    if content.find(old_text, first + 1) >= 0:
        raise ToolExecutionError("ambiguous_patch", "The original text occurs more than once.")
    return content[:first] + new_text + content[first + len(old_text) :]


def _arguments_digest(path: str, old_text: str, new_text: str) -> str:
    encoded = json.dumps(
        {"path": path, "old_text": old_text, "new_text": new_text},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unified_diff(path: str, original: str, replacement: str, *, created: bool) -> str:
    lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        replacement.splitlines(keepends=True),
        fromfile="/dev/null" if created else f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(
        line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n" for line in lines
    )


def _preview_operation(
    workspace_root: Path, arguments: dict[str, object], stopped: threading.Event
) -> dict[str, object]:
    root = _root(workspace_root)
    path, old_text, new_text = _arguments(arguments)
    parts = _target_parts(path)
    if stopped.is_set():
        raise ToolExecutionError("tool_timeout", "The tool reached its time limit.")
    parent_fd = _open_directory(root, parts[:-1])
    try:
        original_target = _read_target(parent_fd, parts[-1])
    finally:
        os.close(parent_fd)
    original_bytes = original_target[0] if original_target is not None else None
    if original_bytes is None:
        if old_text:
            raise ToolExecutionError("target_changed", "The file does not exist.")
        original = ""
        replacement = new_text
    else:
        if not old_text:
            raise ToolExecutionError("target_exists", "This file already exists.")
        original = original_bytes.decode("utf-8")
        replacement = _replace_once(original, old_text, new_text)
    if len(replacement.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolExecutionError("file_too_large", "The edited file exceeds the size limit.")
    diff = _unified_diff(path, original, replacement, created=original_bytes is None)
    if len(diff.encode("utf-8")) > MAX_PATCH_DIFF_BYTES:
        raise ToolExecutionError("patch_too_large", "The patch preview exceeds the size limit.")
    if redact_secrets(diff) != diff:
        raise ToolExecutionError("sensitive_patch", "The patch preview contains a secret.")
    guidance, guidance_hashes = _ancestor_guidance(root, parts)
    return {
        "path": path,
        "target_sha256": (
            hashlib.sha256(original_bytes).hexdigest() if original_bytes is not None else None
        ),
        "target_identity": original_target[1] if original_target is not None else None,
        "arguments_sha256": _arguments_digest(path, old_text, new_text),
        "diff": diff,
        "guidance": guidance,
        "guidance_sha256": guidance_hashes,
    }


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("could not write patch data")
        remaining = remaining[written:]


def _parent_still_attached(root: Path, parent_parts: tuple[str, ...], parent_fd: int) -> bool:
    try:
        current_fd = _open_directory(root, parent_parts)
    except ToolExecutionError:
        return False
    try:
        current = os.fstat(current_fd)
        original = os.fstat(parent_fd)
        return (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    finally:
        os.close(current_fd)


def _apply_operation(
    workspace_root: Path,
    arguments: dict[str, object],
    preview: dict[str, object],
    stopped: threading.Event,
) -> tuple[str, bool]:
    root = _root(workspace_root)
    path, old_text, new_text = _arguments(arguments)
    parts = _target_parts(path)
    expected_digest = preview.get("target_sha256")
    if (
        preview.get("path") != path
        or (expected_digest is not None and not isinstance(expected_digest, str))
        or preview.get("arguments_sha256") != _arguments_digest(path, old_text, new_text)
    ):
        raise ToolExecutionError("approval_invalid", "The approved patch does not match this edit.")
    guidance, guidance_hashes = _ancestor_guidance(root, parts)
    if preview.get("guidance_sha256") != guidance_hashes or preview.get("guidance") != guidance:
        raise ToolExecutionError("target_changed", "Workspace guidance changed after approval.")
    parent_fd = _open_directory(root, parts[:-1])
    temporary_name: str | None = None
    try:
        original_target = _read_target(parent_fd, parts[-1])
        original_bytes = original_target[0] if original_target is not None else None
        original_identity = original_target[1] if original_target is not None else None
        actual_digest = (
            hashlib.sha256(original_bytes).hexdigest() if original_bytes is not None else None
        )
        if actual_digest != expected_digest or original_identity != preview.get("target_identity"):
            raise ToolExecutionError("target_changed", "The file changed after patch approval.")
        if original_bytes is None:
            if old_text:
                raise ToolExecutionError("approval_invalid", "The approved patch is invalid.")
            replacement = new_text
            mode = 0o644
        else:
            if not old_text:
                raise ToolExecutionError("approval_invalid", "The approved patch is invalid.")
            replacement = _replace_once(original_bytes.decode("utf-8"), old_text, new_text)
            assert original_identity is not None
            mode = original_identity["mode"]
        replacement_bytes = replacement.encode("utf-8")
        if len(replacement_bytes) > MAX_FILE_BYTES:
            raise ToolExecutionError("file_too_large", "The edited file exceeds the size limit.")
        if preview.get("diff") != _unified_diff(
            path,
            original_bytes.decode("utf-8") if original_bytes is not None else "",
            replacement,
            created=original_bytes is None,
        ):
            raise ToolExecutionError(
                "approval_invalid", "The approved preview does not match this edit."
            )
        temporary_name = f".trellis-patch-{secrets.token_hex(12)}"
        temp_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(temp_fd, mode)
            _write_all(temp_fd, replacement_bytes)
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        if stopped.is_set():
            raise ToolExecutionError("tool_timeout", "The tool reached its time limit.")
        current_target = _read_target(parent_fd, parts[-1])
        current_bytes = current_target[0] if current_target is not None else None
        current_identity = current_target[1] if current_target is not None else None
        current_digest = (
            hashlib.sha256(current_bytes).hexdigest() if current_bytes is not None else None
        )
        if current_digest != expected_digest or current_identity != preview.get("target_identity"):
            raise ToolExecutionError("target_changed", "The file changed after patch approval.")
        current_guidance, current_guidance_hashes = _ancestor_guidance(root, parts)
        if current_guidance_hashes != preview.get(
            "guidance_sha256"
        ) or current_guidance != preview.get("guidance"):
            raise ToolExecutionError("target_changed", "Workspace guidance changed after approval.")
        if not _parent_still_attached(root, parts[:-1], parent_fd):
            raise ToolExecutionError(
                "target_changed", "The target directory changed after approval."
            )
        if stopped.is_set():
            raise ToolExecutionError("tool_timeout", "The tool reached its time limit.")
        if expected_digest is None:
            try:
                os.link(
                    temporary_name,
                    parts[-1],
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise ToolExecutionError(
                    "target_changed", "The file changed after patch approval."
                ) from None
        else:
            os.replace(temporary_name, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temporary_name = None
        with suppress(OSError):
            os.fsync(parent_fd)
    except OSError:
        raise ToolExecutionError("patch_failed", "The patch could not be applied.") from None
    finally:
        if temporary_name is not None:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)
    return f"{'Created' if expected_digest is None else 'Updated'} {path}.", False


class LocalPatchToolExecutor:
    async def approval_preview(
        self, arguments: Mapping[str, object], workspace_root: Path
    ) -> dict[str, object]:
        return await _run_bounded_file_operation(
            _preview_operation, workspace_root, dict(arguments)
        )

    async def execute(
        self,
        arguments: Mapping[str, object],
        workspace_root: Path,
        approval_preview: dict[str, object] | None,
    ) -> tuple[str, bool]:
        if approval_preview is None:
            raise ToolExecutionError("approval_required", "This patch requires approval.")
        return await _run_bounded_file_operation(
            lambda root, args, stopped: _apply_operation(root, args, approval_preview, stopped),
            workspace_root,
            dict(arguments),
        )
