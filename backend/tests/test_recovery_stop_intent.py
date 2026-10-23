import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from run_fixtures import Provider, Repository, service
from test_recovery_contracts import CONTRACT

from app.application.ports import RecoveryRepository
from app.domain.runtime import RunStatus


@pytest.mark.parametrize("original", [RunStatus.QUEUED, RunStatus.RECOVERING, RunStatus.RUNNING])
def test_stop_before_local_takeover_persists_user_intent_without_dispatch(original, monkeypatch):
    async def scenario():
        repository = Repository()
        repository.run = replace(repository.run, status=original, recovery_contract=CONTRACT)
        runtime = service(repository, Provider())
        requested = replace(repository.run, status=RunStatus.CANCELLING, cancel_requested_at="now")
        control = AsyncMock(return_value=(requested, None))
        runtime._recovery_repository = cast(
            RecoveryRepository, SimpleNamespace(request_recovery_cancellation=control)
        )
        monkeypatch.setattr(runtime, "_can_execute", lambda _: False)
        runtime._recovery_compatibility = AsyncMock(side_effect=AssertionError("no takeover"))
        assert await runtime.cancel_run(repository.run.id) == requested
        control.assert_awaited_once_with(repository.run.id)
        runtime._recovery_compatibility.assert_not_awaited()
        assert not runtime._tasks and not repository.calls

    asyncio.run(scenario())
