import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel, ConfigDict
from run_fixtures import Provider, Repository, service

from app.application.errors import ApplicationError, ProviderError
from app.application.tool_context import ToolExecutionContext
from app.application.tools import ToolRegistration, ToolRegistry
from app.core.config import Settings
from app.domain.capabilities import RuntimeCapabilities
from app.domain.recovery import ToolRecoveryPolicy
from app.domain.runtime import (
    ModelToolCall,
    RunEventType,
    RunStatus,
    ToolCallRecord,
    ToolCallStatus,
)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = "model-supplied-owner"
    run_id: str = "model-supplied-run"


class CaptureExecutor:
    def __init__(self):
        self.context: ToolExecutionContext | None = None
        self.arguments: dict[str, object] | None = None

    async def execute(
        self, name: str, arguments: dict[str, object], context: ToolExecutionContext
    ) -> tuple[str, bool]:
        self.context = context
        self.arguments = arguments
        return "Recorded evidence.", False


def registry(executor, *, requires_workspace=False):
    return ToolRegistry(
        capabilities=RuntimeCapabilities(frozenset({"web_read"})),
        additional_tools=(
            ToolRegistration(
                "web_probe",
                "Inspect a public source.",
                Arguments,
                executor,
                required_capabilities=frozenset({"web_read"}),
                requires_workspace=requires_workspace,
                recovery_policy=ToolRecoveryPolicy.READ_ONLY,
            ),
        ),
    )


class ToolRepository(Repository):
    def __init__(self):
        super().__init__()
        self.run = replace(self.run, status=RunStatus.RUNNING, generation=3)
        self.tool = ToolCallRecord(
            "tool-id",
            self.run.id,
            "assistant-id",
            0,
            "provider-tool-id",
            "web_probe",
            {"user_id": "forged-owner", "run_id": "forged-run"},
            ToolCallStatus.PENDING,
            None,
            None,
            "now",
            None,
        )

    async def list_tool_calls(self, _run_id: str) -> list[ToolCallRecord]:
        return [self.tool]

    async def begin_tool_execution(self, run_id, tool_id, **dispatch):
        self.tool = replace(
            self.tool,
            status=ToolCallStatus.RUNNING,
            dispatch_id=dispatch["dispatch_id"],
            dispatch_attempt=self.tool.dispatch_attempt + 1,
            dispatch_generation=self.run.generation,
            recovery_policy=dispatch["recovery_policy"],
            input_snapshot=dispatch["input_snapshot"],
        )
        return self.tool, await self.append_run_event(run_id, RunEventType.TOOL_CALL, {})

    async def record_tool_result(self, run_id, tool_id, content, **kwargs):
        self.tool = replace(self.tool, status=kwargs["status"])
        return None, self.tool, await self.append_run_event(run_id, RunEventType.TOOL_RESULT, {})


@pytest.mark.parametrize("capability", ["web_search", "web_read", "web_crawl"])
def test_web_capabilities_do_not_enable_workspace_access(capability):
    settings = Settings(_env_file=None, enabled_capabilities=[capability])
    capabilities = RuntimeCapabilities(settings.enabled_capabilities)
    assert not capabilities.workspace_enabled
    assert capabilities.allows(capability)
    assert not capabilities.allows("local_read")


@pytest.mark.parametrize("capability", ["local_read", "local_patch", "local_command"])
def test_local_capabilities_still_enable_workspace_access(capability):
    assert RuntimeCapabilities(frozenset({capability})).workspace_enabled


def test_generic_tool_requires_trusted_context_and_preserves_workspace_requirement():
    async def exercise():
        executor = CaptureExecutor()
        call = ModelToolCall("call", "web_probe", {})
        missing_context = await registry(executor).execute(call, None)
        assert missing_context.error_code == "tool_context_required"
        assert executor.context is None
        missing_workspace = await registry(executor, requires_workspace=True).execute(call, None)
        assert missing_workspace.error_code == "workspace_required"
        assert executor.context is None

    asyncio.run(exercise())


def test_runtime_supplies_saved_dispatch_scope_without_workspace_handling(tmp_path):
    async def exercise():
        repository = ToolRepository()
        executor = CaptureExecutor()
        runtime = service(repository, Provider())
        runtime._tool_registry = registry(executor)
        runtime._capabilities = RuntimeCapabilities(frozenset({"web_read"}))
        runtime._owned_runs.add(repository.run.id)
        bindings = Mock(resolve=AsyncMock(side_effect=AssertionError("Workspace binding was used")))
        runtime._workspace_bindings = bindings
        workspace_execution = Mock()
        workspace_execution.operation.side_effect = AssertionError("Workspace scope was used")
        runtime._workspace_execution = workspace_execution
        execution = replace(
            await runtime._execution_context(repository.run), workspace_root=tmp_path
        )

        assert await runtime._execute_pending_tools(repository.run, execution)
        context = executor.context
        assert context is not None
        assert context.user_id == "user"
        assert context.session_id == repository.run.session_id
        assert context.run_id == repository.run.id
        assert context.tool_call_id == repository.tool.id
        assert context.dispatch_id == repository.tool.dispatch_id
        assert context.dispatch_attempt == 1
        assert context.generation == 3
        assert context.deadline_at.isoformat() == repository.run.deadline_at
        assert context.budget_limits.max_tool_calls == repository.run.max_tool_calls
        assert context.workspace_root is None
        assert executor.arguments == {"user_id": "forged-owner", "run_id": "forged-run"}
        workspace_execution.operation.assert_not_called()
        bindings.resolve.assert_not_awaited()
        assert repository.tool.status is ToolCallStatus.COMPLETED

        repository.run = replace(repository.run, generation=4)
        with pytest.raises(ApplicationError) as stale:
            await context.assert_active()
        assert stale.value.code == "run_lease_lost"

    asyncio.run(exercise())


def test_context_observes_authoritative_cancellation():
    async def exercise():
        repository = ToolRepository()
        executor = CaptureExecutor()
        runtime = service(repository, Provider())
        runtime._tool_registry = registry(executor)
        runtime._capabilities = RuntimeCapabilities(frozenset({"web_read"}))
        runtime._owned_runs.add(repository.run.id)
        execution = await runtime._execution_context(repository.run)
        assert await runtime._execute_pending_tools(repository.run, execution)
        assert executor.context is not None
        repository.run = replace(repository.run, status=RunStatus.CANCELLING)
        with pytest.raises(ProviderError) as cancelled:
            await executor.context.assert_active()
        assert cancelled.value.code == "run_cancelled"

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["cancelled", "stale", "deadline"])
def test_read_only_tools_do_not_hide_activity_failures(failure):
    async def exercise():
        repository = ToolRepository()

        class LosingExecutor:
            async def execute(self, name, arguments, context):
                if failure == "deadline":
                    context = replace(context, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
                    await context.assert_active()
                repository.run = (
                    replace(repository.run, status=RunStatus.CANCELLING)
                    if failure == "cancelled"
                    else replace(repository.run, generation=4)
                )
                await context.assert_active()
                raise AssertionError("Inactive run continued")

        runtime = service(repository, Provider())
        runtime._tool_registry = registry(LosingExecutor())
        runtime._capabilities = RuntimeCapabilities(frozenset({"web_read"}))
        execution = await runtime._execution_context(repository.run)
        with pytest.raises(ApplicationError) as error:
            await runtime._execute_pending_tools(repository.run, execution)
        assert (
            error.value.code
            == {
                "cancelled": "run_cancelled",
                "stale": "run_lease_lost",
                "deadline": "provider_timeout",
            }[failure]
        )
        assert repository.tool.status is ToolCallStatus.RUNNING

    asyncio.run(exercise())


def test_context_stops_at_its_parent_deadline():
    async def exercise():
        repository = ToolRepository()
        executor = CaptureExecutor()
        runtime = service(repository, Provider())
        runtime._tool_registry = registry(executor)
        runtime._capabilities = RuntimeCapabilities(frozenset({"web_read"}))
        execution = await runtime._execution_context(repository.run)
        assert await runtime._execute_pending_tools(repository.run, execution)
        assert executor.context is not None
        check = AsyncMock()
        expired = replace(
            executor.context,
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
            _assert_run_active=check,
        )
        with pytest.raises(TimeoutError):
            await expired.assert_active()
        assert expired.remaining_seconds == 0
        check.assert_not_awaited()

    asyncio.run(exercise())


def test_cancelling_tool_execution_awaits_executor_cleanup():
    async def exercise():
        entered = asyncio.Event()
        closed = asyncio.Event()

        class WaitingExecutor:
            async def execute(self, name, arguments, context):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        repository = ToolRepository()
        runtime = service(repository, Provider())
        runtime._tool_registry = registry(WaitingExecutor())
        runtime._capabilities = RuntimeCapabilities(frozenset({"web_read"}))
        runtime._owned_runs.add(repository.run.id)
        execution = await runtime._execution_context(repository.run)
        task = asyncio.create_task(runtime._execute_pending_tools(repository.run, execution))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert repository.tool.status is ToolCallStatus.RUNNING

    asyncio.run(exercise())


def test_workspace_required_tools_still_enter_workspace_operation(tmp_path):
    async def exercise():
        repository = ToolRepository()
        runtime = service(repository, Provider())
        runtime._tool_registry = registry(CaptureExecutor(), requires_workspace=True)
        entered = []

        @asynccontextmanager
        async def operation(*args):
            entered.append(args)
            yield "workspace-operation"

        runtime._workspace_execution = Mock(operation=operation)
        async with runtime._workspace_tool_operation(
            repository.run, repository.tool, tmp_path
        ) as value:
            assert value == "workspace-operation"
        assert entered[0][0] == repository.run
        assert entered[0][2] == tmp_path

    asyncio.run(exercise())


def test_local_tools_keep_capability_and_exact_approval_checks(tmp_path):
    async def exercise():
        command = AsyncMock()
        command.execute.return_value = ("done", False)
        tools = ToolRegistry(
            command_executor=command,
            capabilities=RuntimeCapabilities(frozenset({"local_command"})),
        )
        call = ModelToolCall("command", "run_command", {"command": "git status"})
        assert (await tools.execute(call, None)).error_code == "workspace_required"
        assert (await tools.execute(call, tmp_path)).error_code == "approval_required"
        mismatched = await tools.execute(call, tmp_path, approved=True, approval_preview={})
        assert mismatched.error_code == "approval_changed"
        preview = await tools.approval_preview(call, tmp_path)
        denied = await tools.execute(
            call,
            tmp_path,
            approved=True,
            approval_preview=preview,
            capabilities=RuntimeCapabilities(),
        )
        assert denied.error_code == "capability_disabled"
        approved = await tools.execute(call, tmp_path, approved=True, approval_preview=preview)
        assert approved.content == "done"
        command.execute.assert_awaited_once()

    asyncio.run(exercise())
