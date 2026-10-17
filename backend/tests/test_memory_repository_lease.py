"""Cloud rows survive a new repository, while execution ownership does not."""

import asyncio
from pathlib import Path

import pytest

from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.domain.runtime import RunStatus
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository


def test_new_repository_can_view_but_cannot_control_previous_run(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "cloud-account"
        original = MemoryRepository(path)
        session = await original.create_session()
        model = (await original.list_models())[0]
        run = await original.create_run(
            session.id, "turn-1", "request-1", "Inspect this project", model
        )
        assert original.owns_run_lease(run.id)

        restarted = MemoryRepository(path)
        assert (await restarted.get_run(run.id)) == run
        assert (await restarted.list_run_events(run.id))[0].sequence == 1
        assert not restarted.owns_run_lease(run.id)
        with pytest.raises(ValueError, match="lease not owned"):
            await restarted.renew_run_lease(run.id)

        service = RunService(
            restarted,
            restarted,
            restarted,
            restarted,
            SecretStore(tmp_path / ".env"),
            {},
            lease_repository=restarted,
        )
        try:
            with pytest.raises(ApplicationError) as denied:
                await service.cancel_run(run.id)
            assert denied.value.code == "run_not_owned"
            saved = await restarted.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.QUEUED
        finally:
            await service.close()

    asyncio.run(exercise())
