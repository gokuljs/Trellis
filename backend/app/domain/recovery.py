from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePath
from typing import cast
from uuid import UUID

from app.domain.context import ContextCheckpoint
from app.domain.models import Message
from app.domain.outputs import AssistantOutput
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    RunMessageRecord,
    RunSnapshot,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
    is_terminal_run_status,
)

RECOVERY_CONTRACT_VERSION = 1
RECOVERY_RUNTIME_VERSION = "checkpoint-runtime-v1"
MAX_RECOVERY_TAKEOVERS = 3
VersionEntries = tuple[tuple[str, str], ...]


def _text(value: object, name: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\0" in value:
        raise ValueError(f"invalid recovery {name}")
    return value


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"invalid recovery {name}")
    return value


def _versions(value: object) -> VersionEntries:
    if not isinstance(value, Mapping) or len(value) > 100:
        raise ValueError("invalid recovery version map")
    return tuple(
        sorted((_text(key, "component"), _text(item, "version")) for key, item in value.items())
    )


@dataclass(frozen=True, slots=True)
class WorkspaceRecoveryReference:
    grant_id: str
    grant_revision: int
    canonical_path: str
    directory_device: int
    directory_inode: int

    def __post_init__(self) -> None:
        UUID(_text(self.grant_id, "grant ID"))
        _integer(self.grant_revision, "grant revision", 1)
        path = _text(self.canonical_path, "workspace path", 4096)
        if not PurePath(path).is_absolute():
            raise ValueError("recovery workspace path must be absolute")
        _integer(self.directory_device, "workspace device")
        _integer(self.directory_inode, "workspace inode")

    def to_dict(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "grant_revision": self.grant_revision,
            "canonical_path": self.canonical_path,
            "directory_device": self.directory_device,
            "directory_inode": self.directory_inode,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WorkspaceRecoveryReference:
        if set(value) != {
            "grant_id",
            "grant_revision",
            "canonical_path",
            "directory_device",
            "directory_inode",
        }:
            raise ValueError("invalid recovery workspace reference")
        return cls(
            _text(value["grant_id"], "grant ID"),
            _integer(value["grant_revision"], "grant revision", 1),
            _text(value["canonical_path"], "workspace path", 4096),
            _integer(value["directory_device"], "workspace device"),
            _integer(value["directory_inode"], "workspace inode"),
        )


@dataclass(frozen=True, slots=True)
class RecoveryCompatibility:
    runtime_version: str
    adapter_versions: VersionEntries
    tool_versions: VersionEntries
    installation_id: str
    workspace: WorkspaceRecoveryReference | None


@dataclass(frozen=True, slots=True)
class RecoveryContract:
    version: int
    runtime_version: str
    adapter_versions: VersionEntries
    tool_versions: VersionEntries
    request_identity: str
    installation_id: str
    workspace: WorkspaceRecoveryReference | None

    def __post_init__(self) -> None:
        _integer(self.version, "contract version", 1)
        _text(self.runtime_version, "runtime version")
        _text(self.request_identity, "request identity")
        UUID(_text(self.installation_id, "installation ID"))
        for entries in (self.adapter_versions, self.tool_versions):
            if not isinstance(entries, tuple) or len(entries) > 100:
                raise ValueError("recovery component versions must be immutable")
            for entry in entries:
                if not isinstance(entry, tuple) or len(entry) != 2:
                    raise ValueError("invalid recovery component version")
                _text(entry[0], "component")
                _text(entry[1], "version")
            if len(dict(entries)) != len(entries):
                raise ValueError("duplicate recovery component version")
        if self.workspace is not None and not isinstance(
            self.workspace, WorkspaceRecoveryReference
        ):
            raise ValueError("invalid recovery workspace reference")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "runtime_version": self.runtime_version,
            "adapter_versions": dict(self.adapter_versions),
            "tool_versions": dict(self.tool_versions),
            "request_identity": self.request_identity,
            "installation_id": self.installation_id,
            "workspace": self.workspace.to_dict() if self.workspace is not None else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RecoveryContract:
        if set(value) != {
            "version",
            "runtime_version",
            "adapter_versions",
            "tool_versions",
            "request_identity",
            "installation_id",
            "workspace",
        }:
            raise ValueError("invalid recovery contract")
        workspace = value["workspace"]
        if workspace is not None and not isinstance(workspace, Mapping):
            raise ValueError("invalid recovery workspace reference")
        return cls(
            _integer(value["version"], "contract version", 1),
            _text(value["runtime_version"], "runtime version"),
            _versions(value["adapter_versions"]),
            _versions(value["tool_versions"]),
            _text(value["request_identity"], "request identity"),
            _text(value["installation_id"], "installation ID"),
            WorkspaceRecoveryReference.from_dict(cast(Mapping[str, object], workspace))
            if workspace is not None
            else None,
        )

    def incompatibility(self, current: RecoveryCompatibility) -> str | None:
        if self.version != RECOVERY_CONTRACT_VERSION:
            return "incompatible_contract"
        if self.runtime_version != current.runtime_version:
            return "incompatible_runtime"
        for recorded, supported, reason in (
            (self.adapter_versions, current.adapter_versions, "incompatible_adapter"),
            (self.tool_versions, current.tool_versions, "incompatible_tool"),
        ):
            versions = dict(supported)
            if any(versions.get(name) != version for name, version in recorded):
                return reason
        if self.installation_id != current.installation_id:
            return "different_installation"
        if self.workspace is not None and current.workspace is None:
            return "workspace_unavailable"
        if self.workspace != current.workspace:
            return "workspace_changed"
        return None


class ModelCallPurpose(StrEnum):
    GENERATION = "generation"
    PORTABLE_SUMMARY = "summary"
    NATIVE_COMPACTION = "native_compaction"


class ToolRecoveryPolicy(StrEnum):
    READ_ONLY = "read_only"
    PATCH = "patch"
    COMMAND = "command"
    NEVER = "never"


class RecoveryAction(StrEnum):
    STOP = "stop"
    PREPARE_NEXT_MODEL = "prepare_next_model"
    DISPATCH_MODEL = "dispatch_model"
    EXECUTE_TOOL = "execute_tool"
    RECORD_DENIED_TOOL = "record_denied_tool"
    RETRY_READ_TOOL = "retry_read_tool"
    RECONCILE_PATCH = "reconcile_patch"
    AWAIT_APPROVAL = "await_approval"
    CONTINUE_COMPACTION = "continue_compaction"


@dataclass(frozen=True, slots=True)
class RecoveryResolution:
    action: RecoveryAction
    reason: str
    model_call_id: str | None = None
    tool_call_id: str | None = None
    retry_allowed: bool = True
    usage_unknown: bool = False


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    run: RunSnapshot
    model_calls: tuple[ModelCallRecord, ...] = ()
    tool_calls: tuple[ToolCallRecord, ...] = ()
    run_messages: tuple[RunMessageRecord, ...] = ()
    messages: tuple[Message, ...] = ()
    outputs: tuple[AssistantOutput, ...] = ()
    context_checkpoints: tuple[ContextCheckpoint, ...] = ()
    context_revision: int = 0


def _consistent(snapshot: RecoverySnapshot) -> bool:
    run = snapshot.run
    for records in (
        snapshot.model_calls,
        snapshot.tool_calls,
        snapshot.run_messages,
        snapshot.outputs,
    ):
        if len({record.id for record in records}) != len(records):
            return False
        if any(record.run_id != run.id for record in records):
            return False
    calls = {call.id: call for call in snapshot.model_calls}
    if sorted(call.step_index for call in calls.values()) != list(range(1, len(calls) + 1)):
        return False
    if len({message.ordinal for message in snapshot.run_messages}) != len(snapshot.run_messages):
        return False
    assistants = {
        message.id: message for message in snapshot.run_messages if message.role == "assistant"
    }
    tool_results = {
        message.tool_call_id: message for message in snapshot.run_messages if message.role == "tool"
    }
    if len(tool_results) != sum(message.role == "tool" for message in snapshot.run_messages):
        return False
    if any(message.model_call_id not in calls for message in assistants.values()):
        return False
    if len({message.model_call_id for message in assistants.values()}) != len(assistants):
        return False
    tools = {tool.id: tool for tool in snapshot.tool_calls}
    if len({(tool.assistant_message_id, tool.call_index) for tool in tools.values()}) != len(tools):
        return False
    for assistant in assistants.values():
        call = calls[cast(str, assistant.model_call_id)]
        if call.status is not ModelCallStatus.COMPLETED:
            return False
        declared = sorted(
            (tool for tool in tools.values() if tool.assistant_message_id == assistant.id),
            key=lambda tool: tool.call_index,
        )
        if [tool.call_index for tool in declared] != list(range(len(declared))):
            return False
        if tuple((item.provider_call_id, item.name, item.arguments) for item in declared) != tuple(
            (item.id, item.name, item.arguments) for item in assistant.tool_calls
        ):
            return False
        response = call.response_snapshot
        if response is None or response.get("content") != assistant.content:
            return False
        if response.get("tool_calls") != [
            {"id": tool.id, "name": tool.name, "arguments": tool.arguments}
            for tool in assistant.tool_calls
        ]:
            return False
    for call in calls.values():
        purpose = call.request_snapshot.get("purpose")
        if call.status is ModelCallStatus.COMPLETED and purpose == ModelCallPurpose.GENERATION:
            response = call.response_snapshot
            if response is None or not response.get("tool_calls"):
                return False
            if not any(message.model_call_id == call.id for message in assistants.values()):
                return False
        if (
            call.status is ModelCallStatus.COMPLETED
            and purpose == ModelCallPurpose.PORTABLE_SUMMARY
            and not any(
                checkpoint.draft.model_call_id == call.id
                for checkpoint in snapshot.context_checkpoints
            )
        ):
            return False
    for tool in tools.values():
        if tool.assistant_message_id not in assistants:
            return False
        if tool.status not in {
            ToolCallStatus.PENDING,
            ToolCallStatus.RUNNING,
            ToolCallStatus.OUTCOME_UNKNOWN,
        }:
            if tool.id not in tool_results:
                return False
        elif tool.id in tool_results:
            return False
        if tool.status is ToolCallStatus.RUNNING and (
            tool.dispatch_attempt < 1 or tool.dispatched_at is None
        ):
            return False
    if any(tool_id not in tools for tool_id in tool_results):
        return False
    unfinished = False
    for tool in sorted(
        tools.values(),
        key=lambda item: (assistants[item.assistant_message_id].ordinal, item.call_index),
    ):
        if unfinished and tool.dispatch_attempt > 0:
            return False
        if tool.status in {
            ToolCallStatus.PENDING,
            ToolCallStatus.RUNNING,
            ToolCallStatus.OUTCOME_UNKNOWN,
        }:
            unfinished = True
            source = assistants[tool.assistant_message_id].model_call_id
            if any(
                call.step_index > calls[cast(str, source)].step_index for call in calls.values()
            ):
                return False
    if any(
        output.model_call_id not in calls or output.session_id != run.session_id
        for output in snapshot.outputs
    ):
        return False
    if any(message.session_id != run.session_id for message in snapshot.messages):
        return False
    for checkpoint in snapshot.context_checkpoints:
        if checkpoint.run_id != run.id:
            continue
        call = calls.get(checkpoint.draft.model_call_id)
        if call is None or call.status is not ModelCallStatus.COMPLETED:
            return False
        if call.request_snapshot.get("purpose") != ModelCallPurpose.PORTABLE_SUMMARY:
            return False
    active_calls = [
        call
        for call in calls.values()
        if call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}
    ]
    if len(active_calls) > 1:
        return False
    return not active_calls or active_calls[0].step_index == max(
        call.step_index for call in calls.values()
    )


def _tool_resolution(tool: ToolCallRecord) -> RecoveryResolution:
    try:
        policy = ToolRecoveryPolicy(tool.recovery_policy or ToolRecoveryPolicy.NEVER)
    except ValueError:
        policy = ToolRecoveryPolicy.NEVER
    if tool.status is ToolCallStatus.OUTCOME_UNKNOWN:
        return RecoveryResolution(
            RecoveryAction.STOP, "tool_outcome_unknown", tool_call_id=tool.id, retry_allowed=False
        )
    if tool.status is ToolCallStatus.RUNNING:
        if policy is ToolRecoveryPolicy.READ_ONLY and tool.dispatch_attempt == 1:
            return RecoveryResolution(
                RecoveryAction.RETRY_READ_TOOL, "retry_interrupted_read", tool_call_id=tool.id
            )
        if policy is ToolRecoveryPolicy.PATCH:
            return RecoveryResolution(
                RecoveryAction.RECONCILE_PATCH, "reconcile_interrupted_patch", tool_call_id=tool.id
            )
        reason = (
            "read_retry_exhausted"
            if policy is ToolRecoveryPolicy.READ_ONLY
            else "tool_outcome_unknown"
        )
        return RecoveryResolution(
            RecoveryAction.STOP,
            reason,
            tool_call_id=tool.id,
            retry_allowed=policy is ToolRecoveryPolicy.READ_ONLY,
        )
    if tool.approval_decision is ToolApprovalDecision.DENIED:
        return RecoveryResolution(
            RecoveryAction.RECORD_DENIED_TOOL, "reuse_tool_denial", tool_call_id=tool.id
        )
    if (
        policy in {ToolRecoveryPolicy.PATCH, ToolRecoveryPolicy.COMMAND}
        and tool.approval_decision is None
    ):
        return RecoveryResolution(
            RecoveryAction.AWAIT_APPROVAL, "approval_required", tool_call_id=tool.id
        )
    return RecoveryResolution(RecoveryAction.EXECUTE_TOOL, "prepared_tool", tool_call_id=tool.id)


def resolve_recovery(
    snapshot: RecoverySnapshot, compatibility: RecoveryCompatibility, now: datetime
) -> RecoveryResolution:
    run = snapshot.run
    uncertain = next(
        (
            tool
            for tool in snapshot.tool_calls
            if tool.status is ToolCallStatus.OUTCOME_UNKNOWN
            or (
                tool.status is ToolCallStatus.RUNNING
                and tool.recovery_policy not in {"read_only", "patch"}
            )
        ),
        None,
    )
    safe_retry = run.retry_allowed and uncertain is None

    def stop(
        reason: str, *, usage_unknown: bool = False, model_call_id: str | None = None
    ) -> RecoveryResolution:
        return RecoveryResolution(
            RecoveryAction.STOP,
            reason,
            model_call_id,
            uncertain.id if uncertain is not None else run.uncertain_tool_call_id,
            safe_retry,
            usage_unknown,
        )

    if is_terminal_run_status(run.status):
        return stop("terminal_run")
    if run.cancel_requested_at is not None or run.status is RunStatus.CANCELLING:
        return stop("cancellation_requested")
    contract = run.recovery_contract
    if contract is None:
        return stop("legacy_run")
    incompatibility = contract.incompatibility(compatibility)
    if incompatibility is not None:
        return stop(incompatibility)
    if contract.request_identity != run.client_request_id:
        return stop("request_identity_changed")
    try:
        deadline = datetime.fromisoformat(run.deadline_at.replace("Z", "+00:00"))
        if deadline.tzinfo is None or now.tzinfo is None:
            return stop("inconsistent_execution_records")
        if deadline <= now:
            return stop("deadline_exceeded")
    except ValueError:
        return stop("inconsistent_execution_records")
    if run.recovery_count > MAX_RECOVERY_TAKEOVERS:
        return stop("recovery_limit_exceeded")
    if not _consistent(snapshot):
        return stop("inconsistent_execution_records")
    calls = sorted(snapshot.model_calls, key=lambda call: call.step_index)
    for call in calls:
        try:
            ModelCallPurpose(cast(str, call.request_snapshot.get("purpose")))
        except ValueError:
            return stop("inconsistent_execution_records")
        if call.status is ModelCallStatus.COMPLETED and call.response_snapshot is None:
            return stop("inconsistent_execution_records")
    if calls and calls[-1].status is ModelCallStatus.STREAMING:
        return stop("model_outcome_unknown", usage_unknown=True, model_call_id=calls[-1].id)
    outstanding_tools = [
        tool
        for tool in snapshot.tool_calls
        if tool.status
        in {ToolCallStatus.PENDING, ToolCallStatus.RUNNING, ToolCallStatus.OUTCOME_UNKNOWN}
    ]
    if outstanding_tools:
        ordinal = {message.id: message.ordinal for message in snapshot.run_messages}
        outstanding_tools.sort(
            key=lambda tool: (ordinal[tool.assistant_message_id], tool.call_index)
        )
        return _tool_resolution(outstanding_tools[0])
    if not calls:
        return RecoveryResolution(RecoveryAction.PREPARE_NEXT_MODEL, "no_prepared_request")
    latest = calls[-1]
    if latest.status is ModelCallStatus.PENDING:
        return RecoveryResolution(
            RecoveryAction.DISPATCH_MODEL, "prepared_request", model_call_id=latest.id
        )
    purpose = ModelCallPurpose(latest.request_snapshot["purpose"])
    native_saved = latest.status is ModelCallStatus.COMPLETED or (
        latest.status is ModelCallStatus.FAILED
        and latest.error_code == "native_context_invalid"
        and latest.accounted_total_tokens is not None
        and latest.estimated_cost is not None
    )
    if purpose is ModelCallPurpose.NATIVE_COMPACTION and native_saved:
        if not isinstance(latest.request_snapshot.get("compaction_intent"), dict):
            return stop("inconsistent_execution_records")
        return RecoveryResolution(
            RecoveryAction.CONTINUE_COMPACTION, "saved_native_compaction", model_call_id=latest.id
        )
    if purpose is ModelCallPurpose.PORTABLE_SUMMARY and latest.status is ModelCallStatus.COMPLETED:
        if not any(
            checkpoint.draft.model_call_id == latest.id
            for checkpoint in snapshot.context_checkpoints
        ):
            return stop("inconsistent_execution_records")
        return RecoveryResolution(RecoveryAction.PREPARE_NEXT_MODEL, "saved_context_checkpoint")
    if latest.status is not ModelCallStatus.COMPLETED:
        return stop("model_execution_failed", model_call_id=latest.id)
    if not any(message.model_call_id == latest.id for message in snapshot.run_messages):
        return stop("inconsistent_execution_records")
    return RecoveryResolution(RecoveryAction.PREPARE_NEXT_MODEL, "saved_model_and_tool_results")
