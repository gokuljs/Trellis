import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from run_fixtures import Provider, Repository, service

from app.application.errors import ApplicationError
from app.application.run_capacity import RunCapacity
from app.domain.recovery import RECOVERY_RUNTIME_VERSION, RecoveryContract
from app.domain.runtime import RunStatus


def test_stale_candidate_does_not_claim_or_release_a_locally_owned_approval_wait():
    repository = Repository()
    repository.run = replace(
        repository.run,
        status=RunStatus.WAITING_FOR_APPROVAL,
        recovery_contract=RecoveryContract(
            1,
            RECOVERY_RUNTIME_VERSION,
            (),
            (),
            repository.run.client_request_id,
            str(uuid4()),
            None,
        ),
    )
    capacity = RunCapacity(max_active_runs=1, max_active_runs_per_user=1)
    reservation = capacity.reserve(
        "local", repository.run.session_id, repository.run.client_request_id
    )
    capacity.bind(reservation, repository.run.id)
    recovery = Mock()
    recovery.claim_recovery = AsyncMock()
    compatibility = AsyncMock()
    runtime = service(repository, Provider(), capacity=capacity)
    runtime._recovery_repository = recovery
    runtime._recovery_compatibility = compatibility
    runtime._owned_runs.add(repository.run.id)

    async def exercise():
        assert await runtime.recover_candidate(repository.run) == repository.run
        recovery.claim_recovery.assert_not_awaited()
        compatibility.assert_not_awaited()
        with pytest.raises(ApplicationError) as blocked:
            capacity.reserve("another-account", "another-session", "another-request")
        assert blocked.value.code == "run_capacity"
        runtime._owned_runs.clear()
        await runtime.close()

    asyncio.run(exercise())
