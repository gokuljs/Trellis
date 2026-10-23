import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict
from run_fixtures import Provider, Repository, service

from app.application.errors import ApplicationError, ProviderError
from app.application.tools import ToolRegistration, ToolRegistry
from app.domain.capabilities import RuntimeCapabilities
from app.domain.recovery import ToolRecoveryPolicy
from app.domain.runtime import (
    RunEventType,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
)


class ProbeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


def tool_record(name="probe", arguments=None, *, approved=False):
    return ToolCallRecord(
        "e0000000-0000-4000-8000-000000000005",
        "c0000000-0000-4000-8000-000000000003",
        "f0000000-0000-4000-8000-000000000006",
        0,
        "provider-tool",
        name,
        {"value": "yes"} if arguments is None else arguments,
        ToolCallStatus.PENDING,
        ToolApprovalDecision.APPROVED if approved else None,
        "now" if approved else None,
        "now",
        None,
    )


class ToolRepository(Repository):
    def __init__(self, tool=None):
        super().__init__()
        self.run = replace(self.run, status=RunStatus.RUNNING)
        self.tool = tool or tool_record()
        self.results: list[tuple[str, ToolCallStatus]] = []
        self.reject_dispatch = False
        self.lose_dispatch_reply = False
        self.lose_result_reply = False

    async def list_tool_calls(self, _run_id):
        return [self.tool]

    async def begin_tool_execution(
        self,
        run_id,
        tool_call_id,
        *,
        dispatch_id,
        expected_attempt,
        recovery_policy,
        input_snapshot=None,
    ):
        if self.reject_dispatch:
            raise ApplicationError("run_lease_lost", "Ownership changed.")
        assert tool_call_id == self.tool.id
        assert expected_attempt == self.tool.dispatch_attempt
        self.tool = replace(
            self.tool,
            status=ToolCallStatus.RUNNING,
            dispatch_attempt=expected_attempt + 1,
            dispatch_generation=self.run.generation,
            dispatched_at="now",
            recovery_policy=recovery_policy,
            dispatch_id=dispatch_id,
            input_snapshot=input_snapshot,
        )
        event = await self.append_run_event(
            run_id,
            RunEventType.PHASE,
            {"phase": "tool_use", "tool_call_id": tool_call_id},
        )
        if self.lose_dispatch_reply:
            raise ApplicationError("database_unavailable", "Dispatch reply lost.")
        return self.tool, event

    async def record_tool_result(self, run_id, tool_call_id, content, *, status):
        assert tool_call_id == self.tool.id
        self.results.append((content, status))
        self.tool = replace(self.tool, status=status, finished_at="now")
        event = await self.append_run_event(
            run_id, RunEventType.TOOL_RESULT, {"tool_call_id": tool_call_id}
        )
        if self.lose_result_reply:
            raise ApplicationError("database_unavailable", "Result reply lost.")
        return None, self.tool, event

    async def mark_tool_outcome_unknown(self, run_id, tool_call_id, reason):
        assert tool_call_id == self.tool.id
        self.tool = replace(self.tool, status=ToolCallStatus.OUTCOME_UNKNOWN)
        self.run = replace(self.run, retry_allowed=False)
        return self.tool


class ProbeExecutor:
    def __init__(self, repository, *, block=False, fail=False):
        self.repository = repository
        self.entered = asyncio.Event()
        self.effects = 0
        self.block = block
        self.fail = fail

    async def execute(self, name, arguments, workspace_root):
        assert self.repository.tool.status is ToolCallStatus.RUNNING
        assert self.repository.tool.dispatch_attempt == 1
        self.effects += 1
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("Connection lost after changing the remote system.")
        return "saved result", False


def runtime_for(repository, executor, *, command=False):
    runtime = service(repository, Provider())
    capabilities = RuntimeCapabilities(frozenset({"local_command"}))
    runtime._capabilities = capabilities
    runtime._tool_registry = (
        ToolRegistry(command_executor=executor, capabilities=capabilities)
        if command
        else ToolRegistry(
            additional_tools=(ToolRegistration("probe", "Probe", ProbeArguments, executor),)
        )
    )
    return runtime


def test_executor_sees_durable_dispatch_before_any_effect():
    async def exercise():
        repository = ToolRepository()
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor)
        assert await runtime._execute_pending_tools(repository.run, None)
        assert executor.effects == 1
        assert repository.tool.recovery_policy == "never"
        assert repository.tool.status is ToolCallStatus.COMPLETED
        assert repository.results == [("saved result", ToolCallStatus.COMPLETED)]

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["reject_dispatch", "lose_dispatch_reply"])
def test_dispatch_without_confirmed_ownership_never_invokes_executor(failure):
    async def exercise():
        repository = ToolRepository()
        setattr(repository, failure, True)
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor)
        with pytest.raises(ApplicationError):
            await runtime._execute_pending_tools(repository.run, None)
        assert executor.effects == 0
        assert not repository.results

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["lease_lost", "cancelling", "cancel_requested"])
def test_publishing_dispatch_cannot_allow_execution_after_ownership_or_cancellation_changes(change):
    async def exercise():
        repository = ToolRepository()
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor)
        ownership = {"current": True}
        runtime._lease_repository = SimpleNamespace(
            owns_run_lease=lambda _run_id: ownership["current"],
            renew_run_lease=AsyncMock(side_effect=lambda _run_id: repository.run),
        )

        async def publish(event):
            assert repository.tool.status is ToolCallStatus.RUNNING
            if change == "lease_lost":
                ownership["current"] = False
            else:
                repository.run = replace(
                    repository.run,
                    status=RunStatus.CANCELLING if change == "cancelling" else RunStatus.RUNNING,
                    cancel_requested_at="now",
                )

        runtime._event_publisher = SimpleNamespace(publish=publish)
        with pytest.raises(ApplicationError):
            await runtime._execute_pending_tools(repository.run, None)
        assert not executor.effects
        assert not repository.results
        if change != "lease_lost":
            assert runtime._can_execute(repository.run.id)

    asyncio.run(exercise())


def test_invalid_arguments_are_rejected_before_dispatch():
    async def exercise():
        repository = ToolRepository(tool_record(arguments={"value": 42}))
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor)
        assert await runtime._execute_pending_tools(repository.run, None)
        assert executor.effects == 0
        assert repository.tool.dispatch_attempt == 0
        assert repository.results == [
            ("invalid_tool_arguments: Tool arguments are invalid.", ToolCallStatus.FAILED)
        ]

    asyncio.run(exercise())


def test_cancelled_dispatched_command_keeps_unknown_outcome(tmp_path):
    async def exercise():
        arguments = {"command": "echo test", "cwd": ".", "timeout_seconds": 120}
        repository = ToolRepository(
            replace(
                tool_record("run_command", arguments, approved=True),
                approval_preview=arguments,
            )
        )
        executor = ProbeExecutor(repository, block=True)
        runtime = runtime_for(repository, executor, command=True)
        task = asyncio.create_task(runtime._execute_pending_tools(repository.run, tmp_path))
        await asyncio.wait_for(executor.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        repository.run = replace(repository.run, status=RunStatus.CANCELLING)
        await runtime._mark_cancelled(repository.run.id, None)
        assert repository.tool.status is ToolCallStatus.OUTCOME_UNKNOWN
        assert repository.run.status is RunStatus.CANCELLED
        assert not repository.run.retry_allowed
        assert not repository.results

    asyncio.run(exercise())


def test_unexpected_mutating_executor_failure_is_not_a_fabricated_result():
    async def exercise():
        repository = ToolRepository()
        executor = ProbeExecutor(repository, fail=True)
        runtime = runtime_for(repository, executor)
        with pytest.raises(RuntimeError, match="after changing"):
            await runtime._execute_pending_tools(repository.run, None)
        await runtime._mark_interrupted(repository.run.id, None)
        assert executor.effects == 1
        assert repository.tool.status is ToolCallStatus.OUTCOME_UNKNOWN
        assert not repository.results

    asyncio.run(exercise())


def test_cleanup_after_lost_result_acknowledgment_preserves_saved_result():
    async def exercise():
        repository = ToolRepository()
        repository.lose_result_reply = True
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor)
        with pytest.raises(ApplicationError, match="Result reply lost"):
            await runtime._execute_pending_tools(repository.run, None)
        await runtime._mark_interrupted(repository.run.id, None)
        assert executor.effects == 1
        assert repository.tool.status is ToolCallStatus.COMPLETED
        assert repository.results == [("saved result", ToolCallStatus.COMPLETED)]

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [ToolCallStatus.RUNNING, ToolCallStatus.OUTCOME_UNKNOWN])
def test_existing_unfinished_dispatch_never_silently_continues_the_model_loop(status):
    async def exercise():
        repository = ToolRepository(replace(tool_record(), status=status))
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor)
        with pytest.raises(ProviderError, match="recovery or review"):
            await runtime._execute_pending_tools(repository.run, None)
        assert not executor.effects
        assert not repository.results

    asyncio.run(exercise())


def test_terminal_dispatch_receipt_does_not_execute_again(monkeypatch):
    async def exercise():
        repository = ToolRepository()
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor)

        async def already_committed(run_id, tool_call_id, **kwargs):
            repository.tool = replace(repository.tool, status=ToolCallStatus.COMPLETED)
            return repository.tool, await repository.append_run_event(
                run_id, RunEventType.PHASE, {"phase": "tool_use", "tool_call_id": tool_call_id}
            )

        monkeypatch.setattr(repository, "begin_tool_execution", already_committed)
        assert await runtime._execute_pending_tools(repository.run, None)
        assert not executor.effects
        assert not repository.results

    asyncio.run(exercise())


@pytest.mark.parametrize("mismatch", ["pending", "generation", "dispatch_id"])
def test_inconsistent_dispatch_receipt_cannot_invoke_an_executor(mismatch, monkeypatch):
    async def exercise():
        repository = ToolRepository()
        executor = ProbeExecutor(repository)
        runtime = runtime_for(repository, executor)
        begin = repository.begin_tool_execution

        async def inconsistent_receipt(*args, **kwargs):
            tool, event = await begin(*args, **kwargs)
            if mismatch == "pending":
                tool = replace(tool, status=ToolCallStatus.PENDING)
            elif mismatch == "generation":
                tool = replace(tool, dispatch_generation=repository.run.generation + 1)
            else:
                tool = replace(tool, dispatch_id="another-dispatch")
            return tool, event

        monkeypatch.setattr(repository, "begin_tool_execution", inconsistent_receipt)
        with pytest.raises(ApplicationError):
            await runtime._execute_pending_tools(repository.run, None)
        assert not executor.effects
        assert not repository.results

    asyncio.run(exercise())


def test_cleanup_preserves_an_already_unknown_command():
    async def exercise():
        repository = ToolRepository(
            replace(
                tool_record(),
                status=ToolCallStatus.OUTCOME_UNKNOWN,
                recovery_policy=ToolRecoveryPolicy.COMMAND.value,
            )
        )
        runtime = runtime_for(repository, ProbeExecutor(repository))
        await runtime._mark_cancelled(repository.run.id, None)
        assert repository.tool.status is ToolCallStatus.OUTCOME_UNKNOWN
        assert not repository.results

    asyncio.run(exercise())


def test_recovery_policies_are_explicit_and_custom_tools_default_to_never():
    registry = ToolRegistry()
    assert registry.recovery_policy("read_file") is ToolRecoveryPolicy.READ_ONLY
    assert registry.recovery_policy("list_files") is ToolRecoveryPolicy.READ_ONLY
    assert registry.recovery_policy("search_files") is ToolRecoveryPolicy.READ_ONLY
    assert registry.recovery_policy("inspect_git") is ToolRecoveryPolicy.READ_ONLY
    assert registry.recovery_policy("apply_patch") is ToolRecoveryPolicy.PATCH
    assert registry.recovery_policy("run_command") is ToolRecoveryPolicy.COMMAND
    assert registry.recovery_policy("unknown") is ToolRecoveryPolicy.NEVER
