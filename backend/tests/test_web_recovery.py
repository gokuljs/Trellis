import asyncio
import hashlib
from dataclasses import asdict, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from run_fixtures import Provider, Repository, model_record, run_record, service

from app.application.tools import ToolRegistry
from app.domain.recovery import (
    RECOVERY_RUNTIME_VERSION,
    RecoveryAction,
    RecoveryCompatibility,
    RecoveryContract,
    RecoveryResolution,
    RecoverySnapshot,
    ToolRecoveryPolicy,
    resolve_recovery,
)
from app.domain.runtime import (
    ModelCallStatus,
    ModelToolCall,
    RunEventType,
    RunMessageRecord,
    RunStatus,
    ToolCallRecord,
    ToolCallStatus,
    ToolResult,
)

INSTALLATION = "31000000-0000-4000-8000-000000000001"


class CheckpointRepository(Repository):
    def __init__(self):
        super().__init__()
        self.authorize_tool_retry = AsyncMock()
        self.mark_tool_outcome_unknown = AsyncMock()
        self.record_tool_result = AsyncMock(side_effect=self._save_result)

    async def _save_result(self, run_id, tool_id, content, **kwargs):
        return None, None, await self.append_run_event(run_id, RunEventType.TOOL_RESULT, {})


def checkpoint_snapshot(*, attempt=1, policy="checkpointed", cancelled=False):
    run = replace(run_record(RunStatus.RECOVERING), generation=2)
    run = replace(
        run,
        recovery_contract=RecoveryContract(
            1,
            RECOVERY_RUNTIME_VERSION,
            (),
            (("web_crawl", "v1"),),
            run.client_request_id,
            INSTALLATION,
            None,
        ),
        cancel_requested_at="2026-10-30T00:00:00Z" if cancelled else None,
    )
    requested = ModelToolCall("provider-tool", "web_crawl", {})
    call = replace(
        model_record(),
        status=ModelCallStatus.COMPLETED,
        input_tokens=100,
        output_tokens=10,
        cached_tokens=0,
        cache_creation_tokens=0,
        request_snapshot={"purpose": "generation"},
        response_snapshot={"content": "", "tool_calls": [asdict(requested)]},
    )
    assistant = RunMessageRecord(
        "assistant", run.id, 1, "assistant", "", call.id, None, run.created_at, (requested,)
    )
    tool = ToolCallRecord(
        "tool",
        run.id,
        assistant.id,
        0,
        requested.id,
        requested.name,
        {},
        ToolCallStatus.RUNNING,
        None,
        None,
        run.created_at,
        None,
        dispatch_attempt=attempt,
        dispatch_generation=1,
        dispatched_at=run.created_at,
        recovery_policy=policy,
        dispatch_id="31000000-0000-4000-8000-000000000002",
        input_snapshot={"arguments": {}, "approval_preview": None},
    )
    return RecoverySnapshot(run, (call,), (tool,), (assistant,))


def compatibility():
    return RecoveryCompatibility(
        RECOVERY_RUNTIME_VERSION, (), (("web_crawl", "v1"),), INSTALLATION, None
    )


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_interrupted_checkpointed_tool_resumes_through_its_checkpoint(attempt):
    resolution = resolve_recovery(
        checkpoint_snapshot(attempt=attempt), compatibility(), datetime.now(UTC)
    )
    assert resolution.action is RecoveryAction.RESUME_CHECKPOINTED_TOOL
    assert resolution.reason == "resume_web_checkpoint"
    assert resolution.retry_allowed


def test_checkpoint_recovery_limit_stops_without_marking_unsafe_local_effects():
    resolution = resolve_recovery(
        checkpoint_snapshot(attempt=4), compatibility(), datetime.now(UTC)
    )
    assert resolution.action is RecoveryAction.STOP
    assert resolution.reason == "web_recovery_exhausted"
    assert resolution.retry_allowed


def test_cancelled_checkpointed_run_stops_without_renewing_its_allowance():
    resolution = resolve_recovery(
        checkpoint_snapshot(cancelled=True), compatibility(), datetime.now(UTC)
    )
    assert resolution.action is RecoveryAction.STOP
    assert resolution.reason == "cancellation_requested" and resolution.retry_allowed


@pytest.mark.parametrize(
    "policy,attempt,action,retry",
    [
        ("read_only", 1, RecoveryAction.RETRY_READ_TOOL, True),
        ("read_only", 2, RecoveryAction.STOP, True),
        ("command", 1, RecoveryAction.STOP, False),
    ],
)
def test_checkpoint_support_preserves_existing_local_recovery_rules(policy, attempt, action, retry):
    resolution = resolve_recovery(
        checkpoint_snapshot(policy=policy, attempt=attempt), compatibility(), datetime.now(UTC)
    )
    assert resolution.action is action and resolution.retry_allowed is retry


def test_checkpoint_resume_requests_distinct_retry_authorization_before_new_dispatch():
    async def exercise():
        repository = CheckpointRepository()
        snapshot = checkpoint_snapshot()
        repository.run = replace(snapshot.run, status=RunStatus.RUNNING)
        tool = snapshot.tool_calls[0]
        repository.authorize_tool_retry = AsyncMock(
            return_value=replace(tool, status=ToolCallStatus.PENDING)
        )
        runtime = service(repository, Provider())
        runtime._owned_runs.add(repository.run.id)
        runtime._tool_registry = ToolRegistry()
        runtime._execute_stored_tool = AsyncMock(return_value=True)
        execution = await runtime._execution_context(repository.run)
        resolution = RecoveryResolution(
            RecoveryAction.RESUME_CHECKPOINTED_TOOL, "resume_web_checkpoint", tool_call_id=tool.id
        )
        assert await runtime._recover_tool(
            repository.run,
            tool,
            snapshot,
            resolution,
            execution,
        )
        repository.authorize_tool_retry.assert_awaited_once_with(
            repository.run.id,
            tool.id,
            expected_dispatch_id=tool.dispatch_id,
            expected_attempt=tool.dispatch_attempt,
            reconciliation="checkpoint_resume",
        )
        dispatched = runtime._execute_stored_tool.await_args
        assert dispatched is not None
        assert dispatched.args[1].status is ToolCallStatus.PENDING

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [ToolCallStatus.CANCELLED, ToolCallStatus.TIMED_OUT])
@pytest.mark.parametrize("after_takeover", [False, True])
def test_checkpoint_terminal_results_keep_the_stopped_attempt_identity(status, after_takeover):
    async def exercise():
        repository = CheckpointRepository()
        snapshot = checkpoint_snapshot()
        repository.run = replace(snapshot.run, status=RunStatus.RUNNING)
        tool = replace(snapshot.tool_calls[0], dispatch_generation=1 if after_takeover else 2)
        repository.list_tool_calls = AsyncMock(return_value=[tool])
        runtime = service(repository, Provider())
        await runtime._finish_pending_tools(
            repository.run.id, status, "Stopped within the allowance."
        )
        repository.mark_tool_outcome_unknown.assert_not_awaited()
        saved = repository.record_tool_result.await_args
        assert saved is not None
        assert saved.kwargs["status"] is status
        assert saved.kwargs["result_snapshot"] == {
            "source": "checkpointed_web",
            "dispatch_id": tool.dispatch_id,
            "generation": tool.dispatch_generation,
            "content_sha256": hashlib.sha256(saved.args[2].encode()).hexdigest(),
        }

    asyncio.run(exercise())


def test_checkpoint_success_receipt_hashes_the_redacted_result_actually_saved():
    async def exercise():
        repository = CheckpointRepository()
        snapshot = checkpoint_snapshot()
        repository.run = replace(snapshot.run, status=RunStatus.RUNNING)
        tool = replace(snapshot.tool_calls[0], dispatch_generation=repository.run.generation)
        runtime = service(repository, Provider())
        assert tool.recovery_policy == ToolRecoveryPolicy.CHECKPOINTED
        await runtime._record_tool_result(
            repository.run.id,
            tool,
            ToolResult(
                tool.provider_call_id,
                tool.name,
                "OPENAI_API_KEY=sk-example-private-value",
                True,
                "source_failed",
            ),
        )
        saved = repository.record_tool_result.await_args
        assert saved is not None
        assert "sk-example-private-value" not in saved.args[2]
        assert (
            saved.kwargs["result_snapshot"]["content_sha256"]
            == hashlib.sha256(saved.args[2].encode()).hexdigest()
        )

    asyncio.run(exercise())
