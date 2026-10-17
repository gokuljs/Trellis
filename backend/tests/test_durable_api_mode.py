import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.core.config import Settings
from app.domain.runtime import RunStatus
from app.infrastructure import accounts
from app.infrastructure.server_credentials import ServerCredentialStore
from tests.memory_repository import MemoryRepository, memory_repository_factory
from tests.support import make_settings
from tests.test_runtime_api import StreamingProvider


def test_durable_account_registry_has_no_process_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args):
        raise AssertionError("Durable accounts must not acquire local ownership")

    monkeypatch.setattr(accounts, "_prepare_account_directory", forbidden)
    monkeypatch.setattr(accounts, "_acquire_account_lock", forbidden)

    async def scenario():
        settings = make_settings(tmp_path, openai_api_key="server-key").model_copy(
            update={"durable_execution_enabled": True, "durable_acceptance_enabled": True}
        )
        user = uuid4()
        async with httpx.AsyncClient() as client:
            registries = [
                accounts.AccountRegistry(
                    settings,
                    {},
                    {"openai": StreamingProvider()},
                    cloud_client=client,
                    repository_factory=memory_repository_factory,
                )
                for _ in range(2)
            ]
            try:
                contexts = [
                    await registry.get(
                        user,
                        access_token="short-browser-token",
                        expires_at=datetime.now(UTC) + timedelta(seconds=15),
                    )
                    for registry in registries
                ]
                session = await contexts[0].database.create_session()
                run = await contexts[0].run_service.create_run(session.id, "request", "Hello")
                assert run.status is RunStatus.QUEUED and run.deadline_at is None
                assert not contexts[0].run_service._tasks
                assert not contexts[0].run_service._lease_monitors
                assert contexts[0].run_service._lease_repository is None
                await registries[0].close()
                restored = await contexts[1].run_service.lookup_run(session.id, "request")
                assert restored is not None and restored.id == run.id
                cancelled = await contexts[1].run_service.cancel_run(run.id)
                assert cancelled.status is RunStatus.CANCELLED
            finally:
                for registry in registries:
                    await registry.close()

    asyncio.run(scenario())


def test_paused_durable_acceptance_restores_retry_without_accepting_new_work(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = MemoryRepository(tmp_path)
        session = await database.create_session()
        credentials = ServerCredentialStore(make_settings(tmp_path, openai_api_key="server-key"))
        service = RunService(
            database,
            database,
            database,
            database,
            credentials,
            {"openai": StreamingProvider()},
            acceptance_repository=database,
            durable_execution=True,
        )
        run = await service.create_run(session.id, "request", "Hello")
        service._acceptance_enabled = False
        database.state.models.clear()
        assert (await service.create_run(session.id, "request", "Hello")).id == run.id
        with pytest.raises(ApplicationError) as failure:
            await service.create_run(session.id, "another", "New work")
        assert failure.value.code == "runtime_unavailable"
        await service.resume_decided_approvals()
        await service.close()
        saved = await database.get_run(run.id)
        assert saved is not None and saved.status is RunStatus.QUEUED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "config",
    [
        {"durable_acceptance_enabled": True},
        {"durable_execution_enabled": True, "enabled_capabilities": ["local_read"]},
        {"worker_capacity": 0},
    ],
)
def test_durable_configuration_rejects_execution_bypasses(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(config)
