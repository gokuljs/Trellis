import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from run_fixtures import run_record
from test_workspace_execution import command, execution
from test_workspace_review import review_fixture

from app.domain.runtime import RunStatus, ToolCallStatus
from app.domain.workspaces import WorkspaceReviewBlock
from app.infrastructure.workspace_execution import CommandMarker


def test_a_later_chat_discovers_the_original_command_review(tmp_path):
    async def scenario():
        runtime, repository, tool = review_fixture(tmp_path)
        source = repository.run
        current_session = str(uuid4())
        session = runtime._sessions.get_session.return_value
        runtime._sessions.get_session.return_value = replace(session, id=current_session)
        runtime._workspace_execution.pending_review = AsyncMock(
            return_value=WorkspaceReviewBlock(source.session_id, source.id, tool.id)
        )
        runtime._workspace_execution.reconcile_command_marker = AsyncMock(return_value=False)
        state = await runtime.get_workspace_review(current_session)
        assert state.blocked
        assert state.workspace_path == str(tmp_path)
        assert (state.source_session_id, state.run_id, state.tool) == (
            source.session_id,
            source.id,
            tool,
        )
        assert not runtime._tasks

    asyncio.run(scenario())


def test_another_accounts_block_discloses_no_operation_or_chat_identity(tmp_path):
    async def scenario():
        runtime, repository, _ = review_fixture(tmp_path)
        runtime._workspace_execution.pending_review = AsyncMock(return_value=WorkspaceReviewBlock())
        repository.get_run = AsyncMock()
        state = await runtime.get_workspace_review(repository.run.session_id)
        assert state.blocked and state.reason == "workspace_review_other_account"
        assert (state.source_session_id, state.run_id, state.tool) == (None, None, None)
        assert state.workspace_path is None
        repository.get_run.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["active", "wrong_run", "missing_tool"])
def test_discovery_only_offers_review_for_a_verified_stopped_command(tmp_path, source):
    async def scenario():
        runtime, repository, tool = review_fixture(tmp_path)
        original = repository.run
        runtime._workspace_execution.pending_review = AsyncMock(
            return_value=WorkspaceReviewBlock(original.session_id, original.id, tool.id)
        )
        runtime._workspace_execution.reconcile_command_marker = AsyncMock(return_value=False)
        if source == "active":
            repository.run = replace(original, status=RunStatus.RUNNING)
        elif source == "wrong_run":
            repository.run = replace(original, id=str(uuid4()))
        else:
            repository.list_tool_calls.return_value = []
        state = await runtime.get_workspace_review(original.session_id)
        assert state.blocked and state.tool is None and state.run_id is None

    asyncio.run(scenario())


@pytest.mark.parametrize("saved", ["result", "never_dispatched", "unknown", "active_prepared"])
def test_marker_reconciliation_uses_canonical_execution_evidence(tmp_path, saved):
    owner, root, _ = execution(tmp_path)
    run, tool = run_record(), command()
    metadata = root.stat()
    marker = CommandMarker(
        owner.account_id,
        run.session_id,
        run.id,
        tool.id,
        str(uuid4()),
        1,
        metadata.st_dev,
        metadata.st_ino,
    )
    owner.markers.prepare(marker)
    if saved == "result":
        run = replace(run, status=RunStatus.COMPLETED)
        tool = replace(
            tool,
            status=ToolCallStatus.COMPLETED,
            dispatch_id=marker.dispatch_id,
            dispatch_attempt=1,
            result_snapshot={"dispatch_id": marker.dispatch_id},
        )
    elif saved == "never_dispatched":
        run = replace(run, status=RunStatus.CANCELLED)
        tool = replace(tool, status=ToolCallStatus.CANCELLED)
    elif saved == "unknown":
        run = replace(run, status=RunStatus.INTERRUPTED)
        tool = replace(tool, status=ToolCallStatus.OUTCOME_UNKNOWN, dispatch_attempt=1)

    async def scenario():
        cleared = await owner.reconcile_command_marker(run, tool, root)
        assert cleared is (saved in {"result", "never_dispatched"})
        block = await owner.pending_review(run.session_id, root)
        assert (block is None) is cleared
        if not cleared:
            assert block == WorkspaceReviewBlock(run.session_id, run.id, tool.id)

    asyncio.run(scenario())
