from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from run_fixtures import Provider, Repository, run_record, service

from app.application.errors import ApplicationError
from app.application.run_capacity import RunCapacity
from app.domain.models import Session
from app.domain.runtime import RunEventType, RunSnapshot, RunStatus


class CapacityRepository(Repository):
    def __init__(self) -> None:
        super().__init__()
        self.records = {}
        self.created = 0

    async def match_run_request(self, session_id, request_id, **submission):
        return self.records.get((session_id, request_id))

    async def create_run(self, session_id, turn_id, request_id, *args, **kwargs):
        self.created += 1
        self.run = replace(
            run_record(),
            id=str(uuid4()),
            session_id=session_id,
            turn_id=turn_id,
            client_request_id=request_id,
        )
        self.records[(session_id, request_id)] = self.run
        return self.run

    async def get_run(self, _run_id: str) -> RunSnapshot:
        return next(run for run in self.records.values() if run.id == _run_id)


def capacity_service(repository, **kwargs):
    runtime = service(repository, Provider(), **kwargs)
    cast(AsyncMock, runtime._sessions.get_session).side_effect = lambda session_id: Session(
        session_id, "user", "title", "now", "now", 0
    )
    return runtime


def test_fifth_active_request_is_rejected_before_acceptance(monkeypatch) -> None:
    async def exercise() -> None:
        repository = CapacityRepository()
        runtime = capacity_service(repository)
        monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        for index in range(4):
            await runtime.create_run(f"chat-{index}", f"request-{index}", "hello")
        with pytest.raises(ApplicationError) as rejected:
            await runtime.create_run("session", "request-5", "hello")
        assert rejected.value.code == "run_capacity"
        assert repository.created == 4
        await runtime.close()

    asyncio.run(exercise())


def test_sixteen_slots_are_shared_across_accounts_and_retries_bypass_capacity(monkeypatch) -> None:
    async def exercise() -> None:
        capacity = RunCapacity()
        repositories = [CapacityRepository() for _ in range(5)]
        runtimes = [
            capacity_service(repository, capacity=capacity, owner_id=str(index))
            for index, repository in enumerate(repositories)
        ]
        for runtime in runtimes:
            monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        for runtime in runtimes[:4]:
            for request_id in range(4):
                await runtime.create_run(f"chat-{request_id}", str(request_id), "hello")
        with pytest.raises(ApplicationError) as rejected:
            await runtimes[4].create_run("session", "new", "hello")
        assert rejected.value.code == "run_capacity"
        assert repositories[4].created == 0
        retries = await asyncio.gather(
            *(runtimes[0].create_run("chat-0", "0", "hello") for _ in range(2))
        )
        assert retries[0].id == retries[1].id
        assert repositories[0].created == 4
        first = retries[0]
        event = await repositories[0].transition_run_record(
            first.id, RunStatus.COMPLETED, RunEventType.COMPLETED, {}
        )
        await runtimes[0]._publish(event)
        await runtimes[4].create_run("session", "new", "hello")
        assert repositories[4].created == 1
        await asyncio.gather(*(runtime.close() for runtime in runtimes))

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [RunStatus.WAITING_FOR_APPROVAL, RunStatus.CANCELLING])
def test_waiting_and_stopping_hold_capacity_until_confirmed_terminal(status, monkeypatch) -> None:
    async def exercise() -> None:
        capacity = RunCapacity(max_active_runs=1, max_active_runs_per_user=1)
        repository = CapacityRepository()
        runtime = capacity_service(repository, capacity=capacity)
        monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        monkeypatch.setattr(runtime, "_execute", AsyncMock())
        run = await runtime.create_run("session", "first", "hello")
        repository.records[(run.session_id, "first")] = replace(run, status=status)
        await runtime._run_bounded(run.id)
        current = await runtime.get_run(run.id)
        assert current is not None and current.status is status
        with pytest.raises(ApplicationError) as rejected:
            await runtime.create_run("session", "second", "hello")
        assert rejected.value.code == "run_capacity"
        repository.records[(run.session_id, "first")] = replace(run, status=RunStatus.CANCELLED)
        current = await runtime.get_run(run.id)
        assert current is not None and current.status is RunStatus.CANCELLED
        await runtime.create_run("session", "second", "hello")
        assert repository.created == 2
        await runtime.close()

    asyncio.run(exercise())


def test_rejected_acceptance_releases_capacity_and_concurrent_retries_use_one_slot(monkeypatch):
    async def exercise() -> None:
        capacity = RunCapacity(max_active_runs=1, max_active_runs_per_user=1)
        repository = CapacityRepository()
        runtime = capacity_service(repository, capacity=capacity)
        monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        create = repository.create_run
        monkeypatch.setattr(
            repository, "create_run", AsyncMock(side_effect=ValueError("active run already exists"))
        )
        with pytest.raises(ApplicationError, match="Another run"):
            await runtime.create_run("session", "first", "hello")
        monkeypatch.setattr(repository, "create_run", create)
        first, second = await asyncio.gather(
            runtime.create_run("session", "second", "hello"),
            runtime.create_run("session", "second", "hello"),
        )
        assert first.id == second.id
        assert repository.created == 1
        await runtime.close()

    asyncio.run(exercise())


def test_uncertain_acceptance_holds_capacity_for_the_remaining_lease_window(monkeypatch):
    from app.application import run_capacity

    async def exercise() -> None:
        now = [0]
        monkeypatch.setattr(run_capacity, "monotonic", lambda: now[0])
        capacity = RunCapacity(max_active_runs=1, max_active_runs_per_user=1)
        repository = CapacityRepository()
        runtime = capacity_service(repository, capacity=capacity)
        monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        create = repository.create_run

        async def unknown(*args, **kwargs):
            now[0] = 40
            raise ApplicationError("database_unavailable", "Unknown acceptance outcome")

        monkeypatch.setattr(repository, "create_run", unknown)
        with pytest.raises(ApplicationError, match="Unknown acceptance"):
            await runtime.create_run("session", "first", "hello")
        monkeypatch.setattr(repository, "create_run", create)
        now[0] = 99
        with pytest.raises(ApplicationError, match="active run limit"):
            await runtime.create_run("session", "second", "hello")
        now[0] = 100
        await runtime.create_run("session", "second", "hello")
        assert repository.created == 1
        await runtime.close()

    asyncio.run(exercise())


def test_lost_lease_recovers_saved_interruption_and_frees_capacity_without_browser(monkeypatch):
    async def exercise() -> None:
        capacity = RunCapacity(max_active_runs=1, max_active_runs_per_user=1)
        repository = CapacityRepository()
        runtime = capacity_service(repository, capacity=capacity)
        monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        run = await runtime.create_run("session", "first", "hello")
        key = (run.session_id, "first")

        async def failed_renewal(run_id):
            repository.records[key] = replace(
                run,
                status=RunStatus.RUNNING,
                lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            )
            raise ApplicationError("database_unavailable", "Could not renew")

        async def recover(run_id):
            repository.records[key] = replace(repository.records[key], status=RunStatus.INTERRUPTED)
            return repository.records[key]

        leases = SimpleNamespace(
            renew_run_lease=AsyncMock(side_effect=failed_renewal),
            recover_expired_run=AsyncMock(side_effect=recover),
            owns_run_lease=lambda run_id: True,
        )
        monkeypatch.setattr(runtime, "_lease_repository", leases)
        monkeypatch.setattr(runtime, "_lease_renew_interval_seconds", 0.001)
        await runtime._monitor_lease(run.id)
        assert repository.records[key].status is RunStatus.INTERRUPTED
        leases.recover_expired_run.assert_awaited_once_with(run.id)
        await runtime.create_run("session", "second", "hello")
        assert repository.created == 2
        await runtime.close()

    asyncio.run(exercise())


def test_capacity_error_is_reported_as_429_without_expiring_authentication():
    from test_hosted_configuration import (
        TestClient,
        auth_frame,
        authenticate,
        hosted_settings,
        install_account,
    )

    from app.main import create_app

    app = create_app(hosted_settings())
    with TestClient(app) as client:
        registry = install_account(app)
        registry.get.return_value.run_service.create_run = AsyncMock(
            side_effect=ApplicationError("run_capacity", "Trellis is working at capacity.")
        )
        registry.get.return_value.run_service.lookup_run = AsyncMock(return_value=None)
        with authenticate(client) as websocket:
            websocket.send_json(auth_frame())
            assert "result" in websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "full",
                    "method": "run.start",
                    "params": {"sessionId": "session", "clientRequestId": "new", "content": "hi"},
                }
            )
            assert websocket.receive_json()["error"]["data"] == {
                "code": "run_capacity",
                "status": 429,
            }
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "lookup",
                    "method": "run.lookup",
                    "params": {"sessionId": "session", "clientRequestId": "new"},
                }
            )
            assert websocket.receive_json()["result"] is None
