import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app import worker
from app.core.config import Settings
from app.domain.jobs import ExecutionClaim
from tests.support import make_settings


def configured(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, openai_api_key="server-key").model_copy(
        update={
            "durable_execution_enabled": True,
            "worker_database_url": SecretStr("postgresql://restricted-worker"),
            "worker_capacity": 2,
        }
    )


@pytest.mark.parametrize(
    "change, message",
    [
        ({"durable_execution_enabled": False}, "Enable durable"),
        ({"worker_database_url": None}, "database URL"),
        ({"openai_api_key": None}, "at least one provider"),
        ({"enabled_capabilities": frozenset({"local_read"})}, "chat only"),
    ],
)
def test_entrypoint_rejects_missing_or_unsafe_configuration(tmp_path, change, message):
    with pytest.raises(ValueError, match=message):
        asyncio.run(worker.run_worker(configured(tmp_path).model_copy(update=change)))


def test_entrypoint_builds_scoped_execution_and_closes_pool(tmp_path, monkeypatch):
    calls = []
    claim = ExecutionClaim("job", "run", "worker", "attempt", "capability", "2099-01-01T00:00:00Z")

    class Queue:
        @classmethod
        async def open(cls, dsn, *, max_size):
            assert dsn == "postgresql://restricted-worker" and max_size == 4
            calls.append("open")
            return cls()

        def bind(self, assignment):
            async def authority(provider):
                assert assignment == claim and provider == "openai"
                calls.append("authority")
                return "owner"

            return SimpleNamespace(claim=assignment, assert_provider_access=authority)

        async def close(self):
            calls.append("close")

    class Supervisor:
        def __init__(self, queue, repository_factory, executor_factory, *, policy):
            assert policy.capacity == 2
            self.executor = executor_factory(repository_factory(claim))

        async def run(self, stop):
            assert await self.executor._secret_store.get("openai") == "server-key"
            assert set(self.executor._providers) == {"openai", "anthropic"}
            stop.set()
            calls.append("run")

    monkeypatch.setattr(worker, "PostgresWorkerQueue", Queue)
    monkeypatch.setattr(worker, "WorkerSupervisor", Supervisor)
    asyncio.run(worker.run_worker(configured(tmp_path)))
    assert calls == ["open", "authority", "run", "close"]


def test_entrypoint_closes_pool_when_supervisor_fails(tmp_path, monkeypatch):
    closed = []

    class Queue:
        @classmethod
        async def open(cls, dsn, *, max_size):
            return cls()

        def bind(self, claim):
            raise AssertionError("No execution expected")

        async def close(self):
            closed.append(True)

    class Supervisor:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, stop):
            raise OSError("worker failed")

    monkeypatch.setattr(worker, "PostgresWorkerQueue", Queue)
    monkeypatch.setattr(worker, "WorkerSupervisor", Supervisor)
    with pytest.raises(OSError, match="worker failed"):
        asyncio.run(worker.run_worker(configured(tmp_path), providers={}))
    assert closed == [True]
