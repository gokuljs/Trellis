import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from run_fixtures import Provider, Repository, service

from app.api.auth import VerifiedUser
from app.api.routes.runtime import _run_result
from app.api.routes.sessions import serialize_run_summary
from app.application.errors import ApplicationError
from app.core.config import Settings
from app.domain.models import Session
from app.domain.recovery import RecoveryContract, WorkspaceRecoveryReference
from app.domain.runtime import (
    RunStatus,
    RunSummary,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
)
from app.main import create_app


class ReviewRepository(Repository):
    def __init__(self) -> None:
        super().__init__()
        self.list_tool_calls = AsyncMock()
        self.acknowledge_workspace_review = AsyncMock()
        self.get_latest_run_for_session = AsyncMock()


def review_fixture(tmp_path: Path):
    repository = ReviewRepository()
    tool_id = str(uuid4())
    repository.run = replace(
        repository.run,
        status=RunStatus.INTERRUPTED,
        retry_allowed=False,
        recovery_reason="command_outcome_unknown",
        uncertain_tool_call_id=tool_id,
    )
    tool = ToolCallRecord(
        id=tool_id,
        run_id=repository.run.id,
        assistant_message_id=str(uuid4()),
        call_index=1,
        provider_call_id="command-call",
        name="run_command",
        arguments={"command": "make release", "cwd": ".", "timeout_seconds": 120},
        status=ToolCallStatus.OUTCOME_UNKNOWN,
        approval_decision=ToolApprovalDecision.APPROVED,
        approval_decided_at="2026-10-23T00:00:00+00:00",
        created_at="2026-10-23T00:00:00+00:00",
        finished_at=None,
        dispatch_attempt=1,
        dispatch_generation=0,
        recovery_policy="command",
        dispatch_id=str(uuid4()),
    )
    repository.list_tool_calls = AsyncMock(return_value=[tool])
    repository.acknowledge_workspace_review = AsyncMock(
        return_value=replace(tool, reviewed_at="2026-10-23T01:00:00+00:00")
    )
    runtime = service(repository, Provider())
    session = Session(
        repository.run.session_id,
        "user",
        "title",
        "now",
        "now",
        0,
        str(tmp_path),
    )
    runtime._sessions = AsyncMock()
    runtime._sessions.get_session.return_value = session
    runtime._workspace_bindings = AsyncMock()
    runtime._workspace_bindings.resolve.return_value = tmp_path
    runtime._workspace_execution = AsyncMock()
    runtime._workspace_execution.review_pending.return_value = False
    return runtime, repository, tool


def test_review_acknowledges_before_clearing_the_matching_local_block(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        original = repository.run
        operations = []
        reviewed = replace(tool, reviewed_at="2026-10-23T01:00:00+00:00")

        async def acknowledge(session_id, run_id, tool_id):
            assert (session_id, run_id, tool_id) == (original.session_id, original.id, tool.id)
            operations.append("database")
            return reviewed

        async def clear(session_id, root, tool_id):
            assert (session_id, root, tool_id) == (original.session_id, tmp_path, tool.id)
            assert operations == ["database"]
            operations.append("local")

        repository.acknowledge_workspace_review.side_effect = acknowledge
        runtime._workspace_execution.acknowledge_review.side_effect = clear
        result = await runtime.review_workspace(
            original.session_id, original.id, tool.id, confirmed=True
        )
        assert result == reviewed
        assert operations == ["database", "local"]
        assert repository.run == original
        assert not repository.run.retry_allowed
        assert result.status is ToolCallStatus.OUTCOME_UNKNOWN
        assert result.arguments == tool.arguments
        assert not runtime._tasks

    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed", [False, None, 0, 1, "true"])
def test_review_requires_explicit_boolean_confirmation(tmp_path: Path, confirmed) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        with pytest.raises(ApplicationError):
            await runtime.review_workspace(
                repository.run.session_id, repository.run.id, tool.id, confirmed=confirmed
            )
        repository.acknowledge_workspace_review.assert_not_awaited()
        runtime._workspace_execution.acknowledge_review.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid",
    [
        "missing_session",
        "missing_run",
        "other_session",
        "other_run_id",
        "active_run",
        "wrong_uncertain_reference",
        "missing_tool",
        "tool_other_run",
        "known_tool_result",
        "non_command",
    ],
)
def test_review_rejects_unowned_or_ineligible_operations(tmp_path: Path, invalid: str) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        session_id, run_id = repository.run.session_id, repository.run.id
        if invalid == "missing_session":
            runtime._sessions.get_session.return_value = None
        elif invalid == "missing_run":
            repository.get_run = AsyncMock(return_value=None)
        elif invalid == "other_session":
            repository.run = replace(repository.run, session_id=str(uuid4()))
        elif invalid == "other_run_id":
            run_id = str(uuid4())
            repository.get_run = AsyncMock(return_value=None)
        elif invalid == "active_run":
            repository.run = replace(repository.run, status=RunStatus.RUNNING)
        elif invalid == "wrong_uncertain_reference":
            repository.run = replace(repository.run, uncertain_tool_call_id=str(uuid4()))
        elif invalid == "missing_tool":
            repository.list_tool_calls.return_value = []
        else:
            changed = {
                "tool_other_run": replace(tool, run_id=str(uuid4())),
                "known_tool_result": replace(tool, status=ToolCallStatus.COMPLETED),
                "non_command": replace(tool, name="apply_patch", recovery_policy="patch"),
            }[invalid]
            repository.list_tool_calls.return_value = [changed]
        with pytest.raises(ApplicationError):
            await runtime.review_workspace(session_id, run_id, tool.id, confirmed=True)
        repository.acknowledge_workspace_review.assert_not_awaited()
        runtime._workspace_execution.acknowledge_review.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", ["binding", "grant", "executor"])
def test_review_requires_current_trusted_workspace(tmp_path: Path, unavailable: str) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        if unavailable == "binding":
            runtime._workspace_bindings = None
        elif unavailable == "grant":
            runtime._workspace_bindings.resolve.return_value = None
        else:
            runtime._workspace_execution = None
        with pytest.raises(ApplicationError):
            await runtime.review_workspace(
                repository.run.session_id, repository.run.id, tool.id, confirmed=True
            )
        repository.acknowledge_workspace_review.assert_not_awaited()

    asyncio.run(scenario())


def test_lost_database_acknowledgment_keeps_local_block_until_review_is_retried(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        reviewed = replace(tool, reviewed_at="2026-10-23T01:00:00+00:00")
        repository.acknowledge_workspace_review.side_effect = [
            ApplicationError("database_unavailable", "The review acknowledgment was lost."),
            reviewed,
        ]
        arguments = (repository.run.session_id, repository.run.id, tool.id)
        with pytest.raises(ApplicationError, match="acknowledgment was lost"):
            await runtime.review_workspace(*arguments, confirmed=True)
        runtime._workspace_execution.acknowledge_review.assert_not_awaited()
        assert await runtime.review_workspace(*arguments, confirmed=True) == reviewed
        assert repository.acknowledge_workspace_review.await_count == 2
        runtime._workspace_execution.acknowledge_review.assert_awaited_once_with(
            repository.run.session_id, tmp_path, tool.id
        )
        assert not repository.run.retry_allowed

    asyncio.run(scenario())


def test_local_clear_failure_is_reported_and_the_same_review_can_finish(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        runtime._workspace_execution.acknowledge_review.side_effect = [
            ApplicationError("workspace_review_mismatch", "The local review could not be saved."),
            None,
        ]
        arguments = (repository.run.session_id, repository.run.id, tool.id)
        with pytest.raises(ApplicationError, match="could not be saved"):
            await runtime.review_workspace(*arguments, confirmed=True)
        repository.get_latest_run_for_session.return_value = repository.run
        repository.list_tool_calls.return_value = [
            repository.acknowledge_workspace_review.return_value
        ]
        runtime._workspace_execution.review_pending.return_value = True
        app = review_app(runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            latest = await client.get(
                f"/api/sessions/{repository.run.session_id}/runs/latest",
                headers={"Authorization": "Bearer approved"},
            )
            assert latest.status_code == 200
            assert latest.json()["uncertain_operation"]["review_pending"] is True
        result = await runtime.review_workspace(*arguments, confirmed=True)
        assert result.reviewed_at == "2026-10-23T01:00:00+00:00"
        assert result.status is ToolCallStatus.OUTCOME_UNKNOWN
        assert repository.acknowledge_workspace_review.await_count == 2
        assert runtime._workspace_execution.acknowledge_review.await_count == 2
        assert not repository.run.retry_allowed
        runtime._workspace_execution.review_pending.return_value = False
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            latest = await client.get(
                f"/api/sessions/{repository.run.session_id}/runs/latest",
                headers={"Authorization": "Bearer approved"},
            )
            assert latest.status_code == 200
            assert latest.json()["uncertain_operation"]["review_pending"] is False

    asyncio.run(scenario())


def review_app(runtime):
    settings = Settings(_env_file=None, environment="test")
    user = VerifiedUser(UUID("f0000000-0000-4000-8000-000000000006"), None)

    async def authenticate(authorization):
        if authorization != "Bearer approved":
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        return user

    sessions = SimpleNamespace(get_message=AsyncMock(return_value=None))
    account = SimpleNamespace(
        database=runtime._sessions, run_service=runtime, session_service=sessions
    )
    app = create_app(settings)
    app.state.settings = settings
    app.state.auth_verifier = SimpleNamespace(verify_bearer=AsyncMock(side_effect=authenticate))
    app.state.account_registry = SimpleNamespace(get=AsyncMock(return_value=account))
    return app


def test_review_endpoint_authenticates_and_requires_typed_explicit_confirmation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        app = review_app(runtime)
        payload = {"run_id": repository.run.id, "tool_call_id": tool.id, "confirmed": True}
        url = f"/api/sessions/{repository.run.session_id}/workspace/review"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post(url, json=payload)).status_code == 401
            headers = {"Authorization": "Bearer approved"}
            for changed in (
                {"run_id": repository.run.id, "tool_call_id": tool.id},
                {**payload, "confirmed": "true"},
                {**payload, "confirmed": 1},
                {**payload, "run_id": "not-a-uuid"},
                {**payload, "tool_call_id": "not-a-uuid"},
            ):
                response = await client.post(url, json=changed, headers=headers)
                assert response.status_code == 422
            repository.acknowledge_workspace_review.assert_not_awaited()
            response = await client.post(url, json=payload, headers=headers)
            assert response.status_code == 200
            assert response.json() == {
                "tool_call_id": tool.id,
                "reviewed_at": "2026-10-23T01:00:00+00:00",
            }
            assert not repository.run.retry_allowed

    asyncio.run(scenario())


def test_review_endpoint_rejects_a_run_from_another_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        app = review_app(runtime)
        wrong_session = str(uuid4())
        payload = {"run_id": repository.run.id, "tool_call_id": tool.id, "confirmed": True}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/sessions/{wrong_session}/workspace/review",
                json=payload,
                headers={"Authorization": "Bearer approved"},
            )
        assert response.status_code == 404
        repository.acknowledge_workspace_review.assert_not_awaited()
        runtime._workspace_execution.acknowledge_review.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("workspace_matches", [False, True])
def test_review_checks_original_directory_identity_after_explicit_reselection(
    tmp_path: Path, workspace_matches: bool
) -> None:
    metadata = tmp_path.stat()

    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        original = WorkspaceRecoveryReference(
            str(uuid4()), 1, str(tmp_path), metadata.st_dev, metadata.st_ino
        )
        repository.run = replace(
            repository.run,
            recovery_contract=RecoveryContract(
                1,
                "checkpoint-runtime-v1",
                (("responses", "v1"),),
                (("run_command", "v1"),),
                repository.run.client_request_id,
                str(uuid4()),
                original,
            ),
        )
        runtime._workspace_bindings.reference.return_value = replace(
            original,
            grant_id=str(uuid4()),
            grant_revision=2,
            directory_inode=original.directory_inode
            if workspace_matches
            else original.directory_inode + 1,
        )
        arguments = (repository.run.session_id, repository.run.id, tool.id)
        if workspace_matches:
            await runtime.review_workspace(*arguments, confirmed=True)
            repository.acknowledge_workspace_review.assert_awaited_once_with(*arguments)
        else:
            with pytest.raises(ApplicationError):
                await runtime.review_workspace(*arguments, confirmed=True)
            repository.acknowledge_workspace_review.assert_not_awaited()
            runtime._workspace_execution.acknowledge_review.assert_not_awaited()

    asyncio.run(scenario())


def test_latest_run_preserves_unknown_command_details_for_manual_review(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        repository.get_latest_run_for_session.return_value = repository.run
        reviewed = replace(tool, reviewed_at="2026-10-23T01:00:00+00:00")
        repository.list_tool_calls.return_value = [reviewed]
        app = review_app(runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/sessions/{repository.run.session_id}/runs/latest",
                headers={"Authorization": "Bearer approved"},
            )
        assert response.status_code == 200
        result = response.json()
        assert result["retry_allowed"] is False
        assert result["recovery_reason"] == "command_outcome_unknown"
        assert result["uncertain_tool_call_id"] == tool.id
        assert result["uncertain_operation"] == {
            "tool_call_id": tool.id,
            "name": "run_command",
            "arguments": tool.arguments,
            "reviewed_at": reviewed.reviewed_at,
            "review_pending": False,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "unavailable", ["unreviewed", "binding", "grant", "executor", "session", "read_failure"]
)
def test_review_form_stays_pending_when_local_clearance_cannot_be_verified(
    tmp_path: Path, unavailable: str
) -> None:
    async def scenario() -> None:
        runtime, repository, tool = review_fixture(tmp_path)
        if unavailable != "unreviewed":
            tool = replace(tool, reviewed_at="2026-10-23T01:00:00+00:00")
        if unavailable == "binding":
            runtime._workspace_bindings = None
        elif unavailable == "grant":
            runtime._workspace_bindings.resolve.return_value = None
        elif unavailable == "executor":
            runtime._workspace_execution = None
        elif unavailable == "session":
            runtime._sessions.get_session.return_value = None
        elif unavailable == "read_failure":
            runtime._workspace_execution.review_pending.side_effect = ApplicationError(
                "workspace_review_mismatch", "The command marker could not be verified."
            )
        assert await runtime.workspace_review_pending(repository.run, tool) is True

    asyncio.run(scenario())


def test_unsafe_retry_is_rejected_without_scheduling_or_holding_capacity(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, repository, _ = review_fixture(tmp_path)
        repository.create_run = AsyncMock(side_effect=ValueError("unsafe_retry_requires_review"))
        with pytest.raises(ApplicationError) as error:
            await runtime.create_run(
                repository.run.session_id, "new-attempt", "hello", turn_id=repository.run.turn_id
            )
        assert error.value.code == "unsafe_retry"
        assert not runtime._tasks
        assert not runtime._capacity._reservations
        assert not repository.run.retry_allowed

    asyncio.run(scenario())


def test_recovery_metadata_survives_summary_and_runtime_serialization(tmp_path: Path) -> None:
    _, repository, tool = review_fixture(tmp_path)
    summary = RunSummary(repository.run, 1, 1, None, False, None, False)
    serialized = serialize_run_summary(summary).model_dump()
    assert serialized["retry_allowed"] is False
    assert serialized["recovery_reason"] == "command_outcome_unknown"
    assert serialized["uncertain_tool_call_id"] == tool.id
    runtime_result = _run_result(repository.run)
    assert runtime_result["retryAllowed"] is False
    assert runtime_result["recoveryReason"] == "command_outcome_unknown"
    assert runtime_result["uncertainToolCallId"] == tool.id
