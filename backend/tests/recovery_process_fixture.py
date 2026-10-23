import asyncio
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from run_fixtures import MODEL, model_record, run_record

from app.application.context_budget import input_fingerprint
from app.application.errors import ApplicationError
from app.application.ports import (
    ProfileRepository,
    RecoveryRepository,
    RunLeasePort,
    RunRepository,
    SecretStorePort,
    SessionRepository,
    SettingsRepository,
    StreamingProviderAdapter,
)
from app.application.runs import RunService
from app.application.tools import ToolRegistry
from app.domain.capabilities import RuntimeCapabilities
from app.domain.context import ContextWindow, InputTokenCount
from app.domain.models import Message, Session, UserProfile
from app.domain.outputs import AssistantOutput
from app.domain.recovery import (
    RECOVERY_RUNTIME_VERSION,
    RecoveryCompatibility,
    RecoveryContract,
    RecoverySnapshot,
)
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelContinuationItem,
    ModelStreamEvent,
    ModelToolCall,
    RunEvent,
    RunEventType,
    RunMessageRecord,
    RunSnapshot,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
    is_terminal_run_status,
)
from app.infrastructure.local_patch import LocalPatchToolExecutor
from app.infrastructure.local_tools import LocalReadToolExecutor, read_workspace_guidance
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from app.infrastructure.workspace_execution import CommandMarkerStore, LocalWorkspaceExecution
from app.infrastructure.workspace_grants import InstallationIdentity, WorkspaceGrantStore

CAPABILITIES = RuntimeCapabilities(frozenset({"local_read", "local_patch", "local_command"}))
ACCOUNT = "10000000-0000-4000-8000-000000000001"


class Ledger:
    def __init__(self, path: Path, fault: str = "") -> None:
        self.path = path
        self.fault = fault
        self.calls = []
        self.tools = []
        self.messages = []
        self.outputs = []
        self.events = []
        self.owner = False
        self.stats = {
            "model_dispatches": 0,
            "read_dispatches": 0,
            "patch_dispatches": 0,
            "command_dispatches": 0,
        }
        self.run = replace(run_record(), input_message_id=str(uuid4()), max_total_tokens=1_000_000)
        self.input = Message(
            self.run.input_message_id,
            self.run.session_id,
            self.run.turn_id,
            1,
            "user",
            "Read hello.txt, replace hello with goodbye, and explain.",
            None,
            None,
            "now",
        )
        if path.exists():
            data = json.loads(path.read_text())
            raw = data["run"]
            raw["status"] = RunStatus(raw["status"])
            raw["recovery_contract"] = (
                RecoveryContract.from_dict(raw["recovery_contract"])
                if raw["recovery_contract"]
                else None
            )
            self.run = RunSnapshot(**raw)
            self.input = Message(**data["input"])
            self.stats = data["stats"]
            for row in data["calls"]:
                row["status"] = ModelCallStatus(row["status"])
                self.calls.append(ModelCallRecord(**row))
            for row in data["tools"]:
                row["status"] = ToolCallStatus(row["status"])
                row["approval_decision"] = (
                    ToolApprovalDecision(row["approval_decision"])
                    if row["approval_decision"]
                    else None
                )
                self.tools.append(ToolCallRecord(**row))
            for row in data["messages"]:
                row["tool_calls"] = tuple(ModelToolCall(**item) for item in row["tool_calls"])
                row["continuation_items"] = tuple(
                    ModelContinuationItem(**item) for item in row["continuation_items"]
                )
                self.messages.append(RunMessageRecord(**row))
            self.outputs = [AssistantOutput(**row) for row in data["outputs"]]
            for row in data["events"]:
                row["event_type"] = RunEventType(row["event_type"])
                self.events.append(RunEvent(**row))

    def save(self):
        run = asdict(self.run)
        run["recovery_contract"] = (
            self.run.recovery_contract.to_dict() if self.run.recovery_contract else None
        )
        data = {
            "run": run,
            "input": asdict(self.input),
            "stats": self.stats,
            "calls": [asdict(item) for item in self.calls],
            "tools": [asdict(item) for item in self.tools],
            "messages": [asdict(item) for item in self.messages],
            "outputs": [asdict(item) for item in self.outputs],
            "events": [asdict(item) for item in self.events],
        }
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def die(self, name):
        if self.fault == name:
            os._exit(73)

    async def get_run(self, run_id):
        return self.run

    async def list_model_calls(self, run_id):
        return self.calls.copy()

    async def list_tool_calls(self, run_id):
        return self.tools.copy()

    async def list_run_messages(self, run_id):
        return self.messages.copy()

    async def list_run_events(self, run_id, after_sequence, *, limit=500):
        return [event for event in self.events if event.sequence > after_sequence][:limit]

    async def append_run_event(self, run_id, event_type, data):
        event = RunEvent(run_id, len(self.events) + 1, event_type, 1, data, "now")
        self.events.append(event)
        self.run = replace(self.run, last_event_sequence=event.sequence)
        self.save()
        return event

    async def transition_run_record(self, run_id, status, event_type, data, **fields):
        fields = {
            key: value for key, value in fields.items() if key in self.run.__dataclass_fields__
        }
        self.run = replace(self.run, status=status, **fields)
        return await self.append_run_event(run_id, event_type, data)

    def owns_run_lease(self, run_id):
        return self.owner

    async def renew_run_lease(self, run_id):
        if not self.owner:
            raise ApplicationError("run_lease_lost", "Ownership changed.")
        return self.run

    async def claim_recovery(self, run, installation_id):
        if is_terminal_run_status(self.run.status):
            return self.run, "terminal_run"
        if run.generation != self.run.generation:
            return self.run, "generation_changed"
        self.owner = True
        self.run = replace(
            self.run,
            generation=self.run.generation + 1,
            recovery_count=self.run.recovery_count + 1,
            status=RunStatus.CANCELLING if self.run.cancel_requested_at else RunStatus.RECOVERING,
        )
        await self.append_run_event(
            run.id,
            RunEventType.RECOVERING,
            {"status": self.run.status.value, "generation": self.run.generation},
        )
        return self.run, None

    async def read_recovery_snapshot(self, run_id):
        return RecoverySnapshot(
            self.run,
            tuple(self.calls),
            tuple(self.tools),
            tuple(self.messages),
            (self.input,),
            tuple(self.outputs),
        )

    async def finish_recovery(self, run_id):
        waiting = next(
            (
                tool
                for tool in self.tools
                if tool.approval_preview is not None and tool.approval_decision is None
            ),
            None,
        )
        self.run = replace(
            self.run, status=RunStatus.WAITING_FOR_APPROVAL if waiting else RunStatus.RUNNING
        )
        return self.run, await self.append_run_event(
            run_id, RunEventType.RESUMED, {"status": self.run.status.value}
        )

    async def relinquish_run(self, run_id):
        self.owner = False
        self.save()
        return self.run

    async def stop_recovery(self, run_id, resolution):
        unknown = []
        for tool in self.tools:
            if tool.dispatch_attempt and tool.status in {
                ToolCallStatus.PENDING,
                ToolCallStatus.RUNNING,
                ToolCallStatus.OUTCOME_UNKNOWN,
            }:
                tool = replace(tool, status=ToolCallStatus.OUTCOME_UNKNOWN)
                if tool.recovery_policy != "read_only":
                    unknown.append(tool)
            self.tools = [tool if previous.id == tool.id else previous for previous in self.tools]
        self.calls = [
            replace(
                call,
                status=ModelCallStatus.CANCELLED,
                error_code="model_outcome_unknown",
                accounted_total_tokens=None,
                estimated_cost=None,
            )
            if call.status is ModelCallStatus.STREAMING
            else call
            for call in self.calls
        ]
        self.outputs = [
            replace(output, status="partial", stop_reason=resolution.reason)
            if output.status == "streaming"
            else output
            for output in self.outputs
        ]
        status = RunStatus.CANCELLED if self.run.cancel_requested_at else RunStatus.INTERRUPTED
        self.run = replace(
            self.run,
            status=status,
            retry_allowed=resolution.retry_allowed and not unknown,
            uncertain_tool_call_id=unknown[0].id if unknown else None,
            recovery_reason=resolution.reason,
        )
        event = await self.append_run_event(
            run_id,
            RunEventType.CANCELLED if status is RunStatus.CANCELLED else RunEventType.INTERRUPTED,
            {"code": resolution.reason, "retry_allowed": self.run.retry_allowed},
        )
        self.owner = False
        return self.run, (event,)

    async def load_context_window(self, session_id):
        return ContextWindow(None, (self.input,))

    async def load_context_checkpoint(self, session_id):
        return None

    async def get_turn_messages(self, session_id, turn_id):
        return [self.input]

    async def create_model_call(self, run_id, step_index, model, snapshot):
        call = replace(
            model_record(), id=str(uuid4()), step_index=step_index, request_snapshot=snapshot
        )
        self.calls.append(call)
        self.save()
        self.die("prepared_model")
        return call

    async def update_model_call(self, call_id, status, **fields):
        fields = {
            key: value
            for key, value in fields.items()
            if key in ModelCallRecord.__dataclass_fields__
        }
        call = next(call for call in self.calls if call.id == call_id)
        call = replace(call, status=status, **fields)
        self.calls = [call if previous.id == call_id else previous for previous in self.calls]
        self.save()
        return call

    async def append_assistant_batch(self, run_id, call_id, index, text):
        previous = next(
            (output for output in self.outputs if output.model_call_id == call_id), None
        )
        if previous:
            output = replace(previous, content=previous.content + text, next_batch_index=index + 1)
            self.outputs = [output if item.id == output.id else item for item in self.outputs]
        else:
            output = AssistantOutput(
                str(uuid4()),
                self.run.session_id,
                self.run.turn_id,
                run_id,
                call_id,
                len(self.calls),
                text,
                "streaming",
                index + 1,
                None,
                None,
                "now",
            )
            self.outputs.append(output)
        event = await self.append_run_event(run_id, RunEventType.ASSISTANT_DELTA, {"text": text})
        self.die("streaming_model")
        return output, event

    async def commit_generation(
        self, run_id, call_id, completion, *, assistant_message=None, final_content=None
    ):
        await self.update_model_call(call_id, ModelCallStatus.COMPLETED, **completion)
        created = []
        if assistant_message:
            message = RunMessageRecord(
                str(uuid4()),
                run_id,
                len(self.messages) + 1,
                "assistant",
                assistant_message.content,
                call_id,
                None,
                "now",
                assistant_message.tool_calls,
                assistant_message.continuation_items,
            )
            self.messages.append(message)
            for index, tool in enumerate(assistant_message.tool_calls):
                saved = ToolCallRecord(
                    str(uuid4()),
                    run_id,
                    message.id,
                    index,
                    tool.id,
                    tool.name,
                    tool.arguments,
                    ToolCallStatus.PENDING,
                    None,
                    None,
                    "now",
                    None,
                    recovery_policy={
                        "read_file": "read_only",
                        "apply_patch": "patch",
                        "run_command": "command",
                    }[tool.name],
                )
                self.tools.append(saved)
                created.append(saved)
        self.outputs = [
            replace(output, status="complete") if output.model_call_id == call_id else output
            for output in self.outputs
        ]
        events = [
            await self.append_run_event(
                run_id, RunEventType.MODEL_COMPLETED, {"model_call_id": call_id}
            )
        ]
        if final_content is not None:
            self.run = replace(self.run, status=RunStatus.COMPLETED)
            events.append(
                await self.append_run_event(run_id, RunEventType.COMPLETED, {"status": "completed"})
            )
        self.save()
        if final_content is not None:
            self.die("final_publication")
        else:
            self.die("completed_model")
        if self.fault == "lost_generation_ack":
            raise ApplicationError("database_unavailable", "The acknowledgment was lost.")
        if self.fault == "invalid_generation_ack":
            raise RuntimeError("Supabase returned invalid JSON")
        return (
            next(call for call in self.calls if call.id == call_id),
            tuple(created),
            tuple(events),
        )

    async def request_tool_approval(self, run_id, tool_id, preview):
        tool = next(tool for tool in self.tools if tool.id == tool_id)
        tool = replace(tool, approval_preview=preview)
        self.tools = [tool if previous.id == tool_id else previous for previous in self.tools]
        self.run = replace(self.run, status=RunStatus.WAITING_FOR_APPROVAL)
        event = await self.append_run_event(
            run_id,
            RunEventType.TOOL_APPROVAL_REQUESTED,
            {"tool_call_id": tool_id, "preview": preview},
        )
        self.die("approval_wait")
        return tool, event

    async def resume_approved_run(self, run_id):
        self.run = replace(self.run, status=RunStatus.RUNNING)
        return self.run, await self.append_run_event(
            run_id, RunEventType.RESUMED, {"status": "running"}
        )

    async def begin_tool_execution(
        self,
        run_id,
        tool_id,
        *,
        dispatch_id,
        expected_attempt,
        recovery_policy,
        input_snapshot=None,
    ):
        tool = next(tool for tool in self.tools if tool.id == tool_id)
        assert expected_attempt == tool.dispatch_attempt
        tool = replace(
            tool,
            status=ToolCallStatus.RUNNING,
            dispatch_attempt=expected_attempt + 1,
            dispatch_generation=self.run.generation,
            dispatched_at=datetime.now(UTC).isoformat(),
            recovery_policy=recovery_policy,
            dispatch_id=dispatch_id,
            input_snapshot=input_snapshot,
        )
        self.tools = [tool if previous.id == tool_id else previous for previous in self.tools]
        event = await self.append_run_event(run_id, RunEventType.PHASE, {"phase": "tool_use"})
        if tool.name == "read_file":
            self.die("dispatched_read")
        elif tool.name == "apply_patch":
            self.die("dispatched_patch")
        return tool, event

    async def authorize_tool_retry(self, run_id, tool_id, **fields):
        tool = next(tool for tool in self.tools if tool.id == tool_id)
        assert fields["expected_dispatch_id"] == tool.dispatch_id
        assert fields["expected_attempt"] == tool.dispatch_attempt
        tool = replace(tool, status=ToolCallStatus.PENDING)
        self.tools = [tool if previous.id == tool_id else previous for previous in self.tools]
        self.save()
        self.die("retry_authorized")
        return tool

    async def record_tool_result(
        self, run_id, tool_id, content, *, status, result_source="executor", result_snapshot=None
    ):
        tool = next(tool for tool in self.tools if tool.id == tool_id)
        if tool.name == "apply_patch":
            self.die("changed_patch")
        tool = replace(
            tool, status=status, result_source=result_source, result_snapshot=result_snapshot
        )
        self.tools = [tool if previous.id == tool_id else previous for previous in self.tools]
        message = RunMessageRecord(
            str(uuid4()), run_id, len(self.messages) + 1, "tool", content, None, tool_id, "now"
        )
        self.messages.append(message)
        event = await self.append_run_event(
            run_id, RunEventType.TOOL_RESULT, {"tool_call_id": tool_id, "content": content}
        )
        if tool.name == "read_file":
            self.die("between_tools")
        return message, tool, event

    async def mark_tool_outcome_unknown(self, run_id, tool_id, reason):
        tool = next(tool for tool in self.tools if tool.id == tool_id)
        tool = replace(tool, status=ToolCallStatus.OUTCOME_UNKNOWN)
        self.tools = [tool if previous.id == tool_id else previous for previous in self.tools]
        self.save()
        return tool


class Settings:
    async def list_models(self):
        return [MODEL]


class Profiles:
    async def get_profile(self):
        return UserProfile(ACCOUNT, None, None, "now", "now")


class Secrets:
    async def get(self, provider_id):
        return "test-key"


class Sessions:
    def __init__(self, ledger, root):
        self.ledger = ledger
        self.root = root

    async def get_session(self, session_id):
        return Session(session_id, ACCOUNT, "Recovery", "now", "now", 1, str(self.root))

    async def get_turn_messages(self, session_id, turn_id):
        return [self.ledger.input]


class Provider:
    def __init__(self, ledger, mode):
        self.ledger = ledger
        self.mode = mode

    async def count_input(self, request, api_key, user_id):
        return InputTokenCount(100, "provider", input_fingerprint(request))

    async def stream(self, request, api_key, user_id):
        self.ledger.stats["model_dispatches"] += 1
        self.ledger.save()
        yield ModelStreamEvent(
            "text_delta",
            text="Saved partial answer. "
            if self.mode == "streaming_model"
            else "I inspected the saved work.",
        )
        if self.mode == "graceful_shutdown":
            await asyncio.Event().wait()
        if len(self.ledger.calls) == 1:
            if self.mode == "unknown_command":
                calls = (
                    ModelToolCall(
                        "command-1",
                        "run_command",
                        {"command": "echo delivered", "cwd": ".", "timeout_seconds": 120},
                    ),
                )
            else:
                calls = (
                    ModelToolCall(
                        "read-1",
                        "read_file",
                        {"path": "hello.txt", "start_line": 1, "max_lines": 5},
                    ),
                    ModelToolCall(
                        "patch-1",
                        "apply_patch",
                        {"path": "hello.txt", "old_text": "hello", "new_text": "goodbye"},
                    ),
                )
            for call in calls:
                yield ModelStreamEvent("tool_call", tool_call=call)
        yield ModelStreamEvent(
            "usage", input_tokens=100, output_tokens=10, cached_tokens=0, cache_creation_tokens=0
        )
        yield ModelStreamEvent(
            "completed", finish_kind="tool" if len(self.ledger.calls) == 1 else "final"
        )


class ReadExecutor(LocalReadToolExecutor):
    def __init__(self, ledger):
        self.ledger = ledger

    async def execute(self, name, arguments, workspace_root):
        self.ledger.stats["read_dispatches"] += 1
        self.ledger.save()
        return await super().execute(name, arguments, workspace_root)


class PatchExecutor(LocalPatchToolExecutor):
    def __init__(self, ledger):
        self.ledger = ledger

    async def execute(self, arguments, workspace_root, approval_preview):
        self.ledger.stats["patch_dispatches"] += 1
        self.ledger.save()
        return await super().execute(arguments, workspace_root, approval_preview)


class CommandExecutor:
    def __init__(self, ledger):
        self.ledger = ledger

    async def execute(self, name, arguments, workspace_root):
        self.ledger.stats["command_dispatches"] += 1
        self.ledger.save()
        (workspace_root / "delivered.txt").write_text("Delivered once.")
        self.ledger.die("unknown_command")
        return "delivered", False


async def run(path: Path, phase: str, fault: str):
    ledger = Ledger(path, fault)
    root = path.parent / "workspace"
    root.mkdir(exist_ok=True)
    private = path.parent / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    account = private / ACCOUNT
    account.mkdir(mode=0o700, exist_ok=True)
    installation = InstallationIdentity.load(private, computer_id="a" * 64)
    grants = WorkspaceGrantStore(account, ACCOUNT, installation)
    if phase == "start":
        (root / "hello.txt").write_text("hello world\n")
        grants.select(ledger.run.session_id, root)
        grant = grants.resolve(ledger.run.session_id, str(root))
        assert grant is not None
        ledger.run = replace(
            ledger.run,
            recovery_contract=RecoveryContract(
                1,
                RECOVERY_RUNTIME_VERSION,
                ((MODEL.adapter_kind, "v1"),),
                (("apply_patch", "v1"), ("read_file", "v1"), ("run_command", "v1")),
                ledger.run.client_request_id,
                installation.installation_id,
                grant.reference(),
            ),
        )
        ledger.save()
    bindings = LocalWorkspaceBindings(grants)
    execution = LocalWorkspaceExecution(
        bindings, CommandMarkerStore(private, installation), ACCOUNT
    )

    async def compatibility(run):
        return RecoveryCompatibility(
            RECOVERY_RUNTIME_VERSION,
            ((MODEL.adapter_kind, "v1"),),
            (("apply_patch", "v1"), ("read_file", "v1"), ("run_command", "v1")),
            installation.installation_id,
            await bindings.reference(run.session_id, str(root)),
        )

    runtime = RunService(
        cast(RunRepository, ledger),
        cast(SessionRepository, Sessions(ledger, root)),
        cast(SettingsRepository, Settings()),
        cast(ProfileRepository, Profiles()),
        cast(SecretStorePort, Secrets()),
        {MODEL.adapter_kind: cast(StreamingProviderAdapter, Provider(ledger, fault))},
        tool_registry=ToolRegistry(
            ReadExecutor(ledger),
            patch_executor=PatchExecutor(ledger),
            command_executor=CommandExecutor(ledger),
            capabilities=CAPABILITIES,
        ),
        workspace_guidance_reader=read_workspace_guidance,
        workspace_bindings=bindings,
        workspace_execution=execution,
        lease_repository=cast(RunLeasePort, ledger),
        recovery_repository=cast(RecoveryRepository, ledger),
        recovery_compatibility=compatibility,
        capabilities=CAPABILITIES,
    )
    if phase == "start":
        ledger.owner = True
        runtime._schedule(ledger.run.id)
    else:
        await runtime.recover_candidate(ledger.run)
    if fault == "queued_shutdown":
        await runtime.close()
        ledger.save()
        return
    if fault == "graceful_shutdown":
        for _ in range(200):
            if ledger.outputs:
                break
            await asyncio.sleep(0.005)
        assert ledger.outputs
        await runtime.close()
        ledger.save()
        return
    await runtime.wait_for_run(ledger.run.id)
    if phase == "approve" or (
        phase == "start"
        and fault
        not in {
            "approval_wait",
            "prepared_model",
            "completed_model",
            "between_tools",
            "streaming_model",
            "dispatched_read",
            "lost_generation_ack",
        }
    ):
        for tool in ledger.tools:
            if tool.status is ToolCallStatus.PENDING and tool.approval_preview is not None:
                ledger.tools = [
                    replace(
                        item,
                        approval_decision=ToolApprovalDecision.APPROVED,
                        approval_decided_at="now",
                    )
                    if item.id == tool.id
                    else item
                    for item in ledger.tools
                ]
        ledger.save()
        if ledger.run.status is RunStatus.WAITING_FOR_APPROVAL:
            runtime._schedule(ledger.run.id)
            await runtime.wait_for_run(ledger.run.id)
    await runtime.close()
    ledger.save()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
