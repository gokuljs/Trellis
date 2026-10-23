"""Pinned, serialized local execution and durable command uncertainty."""

import hashlib
import json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from app.application.errors import ApplicationError
from app.application.ports import WorkspaceOperationPort
from app.application.tools import ToolExecutionError, ToolRegistry
from app.domain.capabilities import RuntimeCapabilities
from app.domain.runtime import (
    ModelToolCall,
    RunSnapshot,
    ToolCallRecord,
    ToolCallStatus,
    ToolResult,
)
from app.domain.workspaces import PatchReconciliation
from app.infrastructure.local_patch import _read_target, _target_parts, reconcile_patch
from app.infrastructure.local_tools import (
    LocalReadToolExecutor,
    _open_directory,
    pinned_workspace,
    read_workspace_guidance,
)
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings, _off_loop
from app.infrastructure.workspace_grants import (
    InstallationIdentity,
    PinnedWorkspace,
    WorkspaceGrantError,
    _private_directory,
    _read,
    _save,
    _storage_lock,
)

_MAX_OBSERVATIONS = 200
_MAX_MARKERS = 1000
_READ_TOOLS = frozenset({"list_files", "search_files", "read_file", "inspect_git"})
_FINISHED_TOOLS = frozenset(
    {
        ToolCallStatus.COMPLETED,
        ToolCallStatus.FAILED,
        ToolCallStatus.DENIED,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.TIMED_OUT,
    }
)


def _failure(code: str) -> ApplicationError:
    messages = {
        "workspace_review_required": "A command in this folder has an unknown outcome. "
        "Review that command before allowing further writes.",
        "workspace_inputs_changed": "A file or workspace input changed since this run saved it.",
        "workspace_grant_unavailable": "The trusted folder permission is unavailable or changed.",
        "workspace_busy": "Another local operation is using this folder. Try again shortly.",
        "workspace_review_mismatch": (
            "The uncertain command does not match this folder and account."
        ),
    }
    return ApplicationError(code, messages.get(code, "Local execution evidence is unavailable."))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _identity(value: object) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid operation identity")
    return value


@dataclass(frozen=True, slots=True)
class CommandMarker:
    account_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    dispatch_id: str
    attempt: int
    device: int
    inode: int

    @property
    def workspace_key(self) -> str:
        return f"{self.device}:{self.inode}"

    @classmethod
    def parse(cls, raw: object) -> CommandMarker:
        if not isinstance(raw, dict):
            raise ValueError("Invalid operation marker")
        values = [
            _identity(raw.get(name))
            for name in ("account_id", "session_id", "run_id", "tool_call_id", "dispatch_id")
        ]
        numbers = [raw.get(name) for name in ("attempt", "device", "inode")]
        if any(type(value) is not int or not 0 <= value < 2**64 for value in numbers):
            raise ValueError("Invalid operation marker")
        attempt, device, inode = cast(list[int], numbers)
        if attempt < 1:
            raise ValueError("Invalid operation attempt")
        return cls(values[0], values[1], values[2], values[3], values[4], attempt, device, inode)


class CommandMarkerStore:
    def __init__(self, directory: Path, installation: InstallationIdentity) -> None:
        self._directory = directory
        self._installation = installation

    def _load(self, descriptor: int) -> dict[str, CommandMarker]:
        saved = _read(descriptor, "workspace-operations.json")
        if saved is None:
            return {}
        if (
            saved.get("version") != 1
            or saved.get("installation_id") != self._installation.installation_id
            or saved.get("computer_id") != self._installation.computer_id
        ):
            raise WorkspaceGrantError("identity_mismatch")
        entries = saved.get("commands")
        if not isinstance(entries, list) or len(entries) > _MAX_MARKERS:
            raise WorkspaceGrantError("corrupt_storage")
        try:
            markers = [CommandMarker.parse(entry) for entry in entries]
            result = {marker.workspace_key: marker for marker in markers}
            if len(result) != len(markers):
                raise ValueError("Duplicate operation marker")
            return result
        except (ValueError, TypeError) as error:
            raise WorkspaceGrantError("corrupt_storage") from error

    def _save(self, descriptor: int, markers: dict[str, CommandMarker]) -> None:
        if len(markers) > _MAX_MARKERS:
            raise WorkspaceGrantError("storage_limit")
        _save(
            descriptor,
            "workspace-operations.json",
            {
                "version": 1,
                "installation_id": self._installation.installation_id,
                "computer_id": self._installation.computer_id,
                "commands": [asdict(marker) for marker in markers.values()],
            },
        )

    def get(self, device: int, inode: int) -> CommandMarker | None:
        with _private_directory(self._directory) as descriptor:
            return self._load(descriptor).get(f"{device}:{inode}")

    def prepare(self, marker: CommandMarker) -> CommandMarker:
        with (
            _private_directory(self._directory) as descriptor,
            _storage_lock(descriptor, ".workspace-operations.lock"),
        ):
            markers = self._load(descriptor)
            existing = markers.get(marker.workspace_key)
            if existing is not None:
                if (
                    existing.account_id != marker.account_id
                    or existing.run_id != marker.run_id
                    or existing.tool_call_id != marker.tool_call_id
                    or existing.attempt != marker.attempt
                ):
                    raise _failure("workspace_review_required")
                return existing
            markers[marker.workspace_key] = marker
            self._save(descriptor, markers)
            return marker

    def clear(self, marker: CommandMarker) -> None:
        with (
            _private_directory(self._directory) as descriptor,
            _storage_lock(descriptor, ".workspace-operations.lock"),
        ):
            markers = self._load(descriptor)
            existing = markers.get(marker.workspace_key)
            if existing is None:
                return
            if existing != marker:
                raise _failure("workspace_review_mismatch")
            del markers[marker.workspace_key]
            self._save(descriptor, markers)


def _observations(tools: Sequence[ToolCallRecord]) -> list[dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for tool in tools:
        if tool.status not in _FINISHED_TOOLS:
            continue
        snapshot = tool.result_snapshot
        if snapshot is None:
            continue
        entries = snapshot.get("workspace_observations")
        if not isinstance(entries, list) or len(entries) > _MAX_OBSERVATIONS:
            raise _failure("workspace_inputs_changed")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
                raise _failure("workspace_inputs_changed")
            latest[entry["key"]] = entry
    if len(latest) > _MAX_OBSERVATIONS:
        raise _failure("workspace_inputs_changed")
    return list(latest.values())


def _patch_target_hash(root: Path, path: str) -> str | None:
    parts = _target_parts(path)
    descriptor = _open_directory(root, parts[:-1])
    try:
        target = _read_target(descriptor, parts[-1])
        return hashlib.sha256(target[0]).hexdigest() if target is not None else None
    finally:
        os.close(descriptor)


async def _observe(root: Path, observation: dict[str, object]) -> dict[str, object]:
    name = observation.get("name")
    if name == "patch_target":
        arguments = observation.get("arguments")
        if not isinstance(arguments, dict) or not isinstance(arguments.get("path"), str):
            raise _failure("workspace_inputs_changed")
        digest = await _off_loop(lambda: _patch_target_hash(root, arguments["path"]))
        return {**observation, "sha256": digest}
    if name == "workspace_guidance":
        value: object = await read_workspace_guidance(root)
    elif name in _READ_TOOLS and isinstance(observation.get("arguments"), dict):
        result = await ToolRegistry(
            LocalReadToolExecutor(),
            capabilities=RuntimeCapabilities(frozenset({"local_read"})),
        ).execute(
            ModelToolCall(
                "observation", cast(str, name), cast(dict[str, object], observation["arguments"])
            ),
            root,
        )
        if result.is_error:
            raise _failure("workspace_inputs_changed")
        value = (result.content, result.truncated)
    else:
        raise _failure("workspace_inputs_changed")
    return {**observation, "sha256": _digest(value)}


class _WorkspaceOperation:
    def __init__(
        self,
        owner: LocalWorkspaceExecution,
        pinned: PinnedWorkspace,
        run: RunSnapshot,
        tool: ToolCallRecord,
        prior_tools: Sequence[ToolCallRecord],
    ) -> None:
        self._owner = owner
        self._pinned = pinned
        self._run = run
        self._tool = tool
        self._prior_tools = prior_tools
        self._observations = _observations(prior_tools)
        self._marker: CommandMarker | None = None
        self._dispatch_id: str | None = None

    async def reconcile_patch(self) -> PatchReconciliation:
        if self._tool.name != "apply_patch":
            raise _failure("workspace_inputs_changed")
        await self.verify()
        preview = self._tool.approval_preview or {}
        state = await reconcile_patch(self._tool.arguments, preview, self._pinned.path)
        before = preview.get("target_sha256")
        after = preview.get("desired_sha256")
        return PatchReconciliation(
            state,
            before if isinstance(before, str) else None,
            after if isinstance(after, str) else None,
            after
            if state == "desired_state_present" and isinstance(after, str)
            else (before if state == "unchanged_original" and isinstance(before, str) else None),
        )

    async def verify(self) -> None:
        if not await _off_loop(self._pinned.verify):
            raise _failure("workspace_grant_unavailable")

    async def capture_inputs(self) -> dict[str, object]:
        self._observations = _observations(self._prior_tools)
        for observation in self._observations:
            if await _observe(self._pinned.path, observation) != observation:
                raise _failure("workspace_inputs_changed")
        guidance = await _observe(
            self._pinned.path, {"key": "workspace_guidance", "name": "workspace_guidance"}
        )
        self._observations = [
            entry for entry in self._observations if entry["key"] != "workspace_guidance"
        ] + [guidance]
        return {"workspace_observations": self._observations}

    async def mark_dispatch(self, dispatch_id: str) -> str:
        await self.verify()
        if self._tool.name == "run_command":
            marker = CommandMarker(
                self._owner.account_id,
                self._run.session_id,
                self._run.id,
                self._tool.id,
                dispatch_id,
                self._tool.dispatch_attempt + 1,
                self._pinned.grant.device,
                self._pinned.grant.inode,
            )
            self._marker = await _off_loop(lambda: self._owner.markers.prepare(marker))
            dispatch_id = self._marker.dispatch_id
        self._dispatch_id = dispatch_id
        return dispatch_id

    async def capture_result(self, result: ToolResult) -> dict[str, object]:
        await self.verify()
        if self._tool.name in _READ_TOOLS and not result.is_error:
            observation = await _observe(
                self._pinned.path,
                {
                    "key": self._tool.id,
                    "name": self._tool.name,
                    "arguments": self._tool.arguments,
                },
            )
            if observation["sha256"] != _digest((result.content, result.truncated)):
                raise _failure("workspace_inputs_changed")
            self._observations.append(observation)
        elif self._tool.name in {"apply_patch", "run_command"}:
            self._observations = [
                await _observe(self._pinned.path, entry) for entry in self._observations
            ]
            if self._tool.name == "apply_patch" and not result.is_error:
                path = self._tool.arguments.get("path")
                observation = await _observe(
                    self._pinned.path,
                    {"key": f"patch:{path}", "name": "patch_target", "arguments": {"path": path}},
                )
                if observation["sha256"] != (self._tool.approval_preview or {}).get(
                    "desired_sha256"
                ):
                    raise _failure("workspace_inputs_changed")
                self._observations = [
                    entry for entry in self._observations if entry["key"] != observation["key"]
                ] + [observation]
        if len(self._observations) > _MAX_OBSERVATIONS:
            raise _failure("workspace_inputs_changed")
        return {
            "source": "executor",
            "dispatch_id": self._dispatch_id or self._tool.dispatch_id,
            "attempt": self._tool.dispatch_attempt + (self._dispatch_id is not None),
            "workspace_observations": self._observations,
        }

    async def confirm_saved(self) -> None:
        if self._marker is not None:
            await _off_loop(lambda: self._owner.markers.clear(cast(CommandMarker, self._marker)))


class LocalWorkspaceExecution:
    def __init__(
        self, bindings: LocalWorkspaceBindings, markers: CommandMarkerStore, account_id: str
    ) -> None:
        self.bindings = bindings
        self.markers = markers
        self.account_id = _identity(account_id)

    @asynccontextmanager
    async def _locked(
        self, session_id: str, root: Path, *, exclusive: bool
    ) -> AsyncIterator[PinnedWorkspace]:
        pinned = await self.bindings.pin(session_id, str(root))
        if pinned is None:
            raise _failure("workspace_grant_unavailable")
        context = pinned.lock(exclusive=exclusive)
        try:

            def release_cancelled(_: PinnedWorkspace) -> None:
                context.__exit__(None, None, None)

            await _off_loop(context.__enter__, close_cancelled=release_cancelled)
            try:
                with pinned_workspace(root, pinned.descriptor, pinned.verify):
                    yield pinned
            finally:
                await _off_loop(lambda: context.__exit__(None, None, None))
        except WorkspaceGrantError as error:
            raise _failure(
                "workspace_busy"
                if error.code == "workspace_busy"
                else "workspace_grant_unavailable"
            ) from None
        finally:
            pinned.close()

    @asynccontextmanager
    async def guard(
        self, session_id: str, root: Path, *, exclusive: bool = False
    ) -> AsyncIterator[None]:
        async with self._locked(session_id, root, exclusive=exclusive):
            yield

    @asynccontextmanager
    async def operation(
        self,
        run: RunSnapshot,
        tool: ToolCallRecord,
        root: Path,
        prior_tools: Sequence[ToolCallRecord] = (),
    ) -> AsyncIterator[WorkspaceOperationPort]:
        write = tool.name not in _READ_TOOLS
        async with self._locked(run.session_id, root, exclusive=write) as pinned:
            marker = await _off_loop(
                lambda: self.markers.get(pinned.grant.device, pinned.grant.inode)
            )
            if (
                write
                and marker is not None
                and not (
                    marker.account_id == self.account_id
                    and marker.run_id == run.id
                    and marker.tool_call_id == tool.id
                    and tool.status is ToolCallStatus.PENDING
                    and tool.dispatch_attempt == 0
                )
            ):
                raise _failure("workspace_review_required")
            yield _WorkspaceOperation(self, pinned, run, tool, prior_tools)

    async def reconcile_patch(
        self, run: RunSnapshot, tool: ToolCallRecord, root: Path
    ) -> PatchReconciliation:
        async with self.operation(run, tool, root) as operation:
            return await operation.reconcile_patch()

    async def validate_observations(
        self, run: RunSnapshot, tools: Sequence[ToolCallRecord], root: Path
    ) -> None:
        async with self._locked(run.session_id, root, exclusive=True) as pinned:
            marker = await _off_loop(
                lambda: self.markers.get(pinned.grant.device, pinned.grant.inode)
            )
            if marker is not None:
                saved = next((tool for tool in tools if tool.id == marker.tool_call_id), None)
                if (
                    marker.account_id == self.account_id
                    and marker.run_id == run.id
                    and saved is not None
                    and saved.status in _FINISHED_TOOLS
                    and saved.dispatch_id == marker.dispatch_id
                    and saved.result_snapshot is not None
                    and saved.result_snapshot.get("dispatch_id") == marker.dispatch_id
                ):
                    await _off_loop(lambda: self.markers.clear(marker))
                elif marker.run_id != run.id or marker.account_id != self.account_id:
                    raise _failure("workspace_review_required")
            try:
                for observation in _observations(tools):
                    if await _observe(root, observation) != observation:
                        raise _failure("workspace_inputs_changed")
            except ToolExecutionError:
                raise _failure("workspace_inputs_changed") from None

    async def acknowledge_review(self, session_id: str, root: Path, tool_call_id: str) -> None:
        async with self._locked(session_id, root, exclusive=True) as pinned:
            marker = await _off_loop(
                lambda: self.markers.get(pinned.grant.device, pinned.grant.inode)
            )
            if marker is None:
                return
            if (
                marker.account_id != self.account_id
                or marker.session_id != session_id
                or marker.tool_call_id != tool_call_id
            ):
                raise _failure("workspace_review_mismatch")
            await _off_loop(lambda: self.markers.clear(marker))
