import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from run_fixtures import Provider, Repository, plan, service

from app.api.dependencies import get_chat_service, get_session_service, log_owned_chat
from app.application.errors import ApplicationError
from app.application.run_context import RunContext
from app.core.config import Settings
from app.domain.runtime import ModelStreamEvent, RunEventType, RunStatus
from app.main import create_app


@pytest.mark.parametrize(
    "status",
    [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_FOR_APPROVAL, RunStatus.CANCELLING],
)
def test_shutdown_finishes_owned_work_after_its_task_has_returned(status, monkeypatch):
    async def exercise():
        repository = Repository()
        repository.run = replace(repository.run, status=status)
        runtime = service(repository, Provider())
        monkeypatch.setattr(runtime, "_execute", AsyncMock())
        await repository.append_run_event(
            repository.run.id, RunEventType.ASSISTANT_DELTA, {"text": "saved"}
        )
        runtime._schedule(repository.run.id)
        await runtime.wait_for_run(repository.run.id)
        await asyncio.sleep(0)
        assert not runtime._tasks
        await runtime.close()
        expected = RunStatus.CANCELLED if status is RunStatus.CANCELLING else RunStatus.INTERRUPTED
        assert repository.run.status is expected
        assert repository.events[0].data == {"text": "saved"}
        assert repository.events[-1].event_type.value == f"run.{expected.value}"

    asyncio.run(exercise())


@pytest.mark.parametrize("response_lost", [False, True, "database_response_lost"])
def test_stop_closes_a_silent_provider_and_preserves_published_output(response_lost, monkeypatch):
    async def exercise():
        saved = asyncio.Event()
        closed = asyncio.Event()
        repository = Repository()
        append = repository.append_assistant_batch

        async def append_output(*args):
            result = await append(*args)
            saved.set()
            return result

        async def cancel(run_id):
            repository.run = replace(repository.run, status=RunStatus.CANCELLING)
            if response_lost == "database_response_lost":
                raise ApplicationError("database_unavailable", "The cancellation reply was lost.")
            event = (
                None
                if response_lost
                else await repository.append_run_event(
                    run_id, RunEventType.CANCELLATION_REQUESTED, {}
                )
            )
            return repository.run, event

        class SilentProvider:
            async def stream(self, *args):
                try:
                    yield ModelStreamEvent("text_delta", text="Saved partial answer. ")
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        monkeypatch.setattr(repository, "append_assistant_batch", append_output)
        monkeypatch.setattr(repository, "request_run_cancellation", cancel, raising=False)
        monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
        runtime = service(repository, SilentProvider())
        if response_lost == "database_response_lost":
            monkeypatch.setattr(
                runtime,
                "_lease_repository",
                SimpleNamespace(
                    owns_run_lease=lambda run_id: True,
                    renew_run_lease=AsyncMock(side_effect=lambda run_id: repository.run),
                ),
            )
            runtime._lease_renew_interval_seconds = 0.001
        run = await runtime.create_run("session", "request", "hello")
        await asyncio.wait_for(saved.wait(), 1)
        if response_lost == "database_response_lost":
            with pytest.raises(ApplicationError, match="reply was lost"):
                await runtime.cancel_run(run.id)
            await asyncio.wait_for(closed.wait(), 1)
        else:
            await runtime.cancel_run(run.id)
        await asyncio.wait_for(
            asyncio.gather(runtime.wait_for_run(run.id), return_exceptions=True), 1
        )
        assert closed.is_set()
        assert repository.run.status is RunStatus.CANCELLED
        assert any(
            event.data.get("text") == "Saved partial answer. " for event in repository.events
        )
        await runtime.close()

    asyncio.run(exercise())


def test_shutdown_cleans_monitors_and_capacity_when_persistence_fails(monkeypatch):
    async def exercise():
        repository = Repository()
        runtime = service(repository, Provider())
        monkeypatch.setattr(runtime, "_execute", AsyncMock())
        reservation = runtime._capacity.reserve("local", "session", "request")
        runtime._capacity.bind(reservation, repository.run.id)
        runtime._schedule(repository.run.id)
        await runtime.wait_for_run(repository.run.id)

        async def monitor_lease() -> None:
            await asyncio.Event().wait()

        monitor = asyncio.create_task(monitor_lease())
        runtime._lease_monitors[repository.run.id] = monitor
        read = AsyncMock(side_effect=RuntimeError("offline"))
        monkeypatch.setattr(repository, "get_run", read)
        await runtime.close()
        read.assert_awaited_once()
        assert monitor.cancelled()
        assert not runtime._capacity._reservations

    asyncio.run(exercise())


def test_shutdown_blocks_an_approval_that_finishes_after_closing_started(monkeypatch):
    async def exercise():
        repository = Repository()
        repository.run = replace(repository.run, status=RunStatus.WAITING_FOR_APPROVAL)
        runtime = service(repository, Provider())
        execute = AsyncMock()
        monkeypatch.setattr(runtime, "_execute", execute)
        runtime._closing = True
        runtime._schedule(repository.run.id)
        assert repository.run.id in runtime._owned_runs
        assert not runtime._tasks
        await runtime.close()
        assert repository.run.status is RunStatus.INTERRUPTED
        execute.assert_not_awaited()

    asyncio.run(exercise())


def test_approval_waiting_expires_and_completed_runs_do_not_report_lost_leases(monkeypatch, caplog):
    async def exercise():
        repository = Repository()
        repository.run = replace(
            repository.run,
            status=RunStatus.WAITING_FOR_APPROVAL,
            deadline_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
        runtime = service(repository, Provider())
        leases = SimpleNamespace(renew_run_lease=AsyncMock(return_value=repository.run))
        monkeypatch.setattr(runtime, "_lease_repository", leases)
        monkeypatch.setattr(runtime, "_lease_renew_interval_seconds", 0.001)
        await asyncio.wait_for(runtime._monitor_lease(repository.run.id), 1)
        assert repository.run.status is RunStatus.FAILED
        assert repository.events[-1].data["code"] == "provider_timeout"
        await runtime._monitor_lease(repository.run.id)
        repository.run = replace(repository.run, status=RunStatus.RUNNING)

        async def completed_during_renewal(run_id):
            repository.run = replace(repository.run, status=RunStatus.COMPLETED)
            raise ApplicationError("run_lease_lost", "The completed run released its lease.")

        leases.renew_run_lease.side_effect = completed_during_renewal
        await runtime._monitor_lease(repository.run.id)
        assert not any(
            getattr(record, "event", None) == "run.lease.lost" for record in caplog.records
        )

    asyncio.run(exercise())


def test_expired_latest_run_is_interrupted_without_execution(monkeypatch):
    async def exercise():
        repository = Repository()
        repository.run = replace(
            repository.run,
            status=RunStatus.RUNNING,
            lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
        runtime = service(repository, Provider())
        recovered = replace(repository.run, status=RunStatus.INTERRUPTED)
        leases = SimpleNamespace(recover_expired_run=AsyncMock(return_value=recovered))
        monkeypatch.setattr(runtime, "_lease_repository", leases)
        monkeypatch.setattr(
            repository,
            "get_latest_run_for_session",
            AsyncMock(return_value=repository.run),
            raising=False,
        )
        schedule = AsyncMock()
        monkeypatch.setattr(runtime, "_schedule", schedule)
        result = await runtime.get_latest_run_for_session("session")
        assert result is not None and result.status is RunStatus.INTERRUPTED
        leases.recover_expired_run.assert_awaited_once_with(repository.run.id)
        schedule.assert_not_called()

    asyncio.run(exercise())


def test_retired_turn_endpoint_checks_ownership_and_directs_clients_to_websocket():
    app = create_app(Settings(_env_file=None, environment="test"))
    sessions = SimpleNamespace(
        get=AsyncMock(side_effect=lambda session_id: object() if session_id == "owned" else None)
    )
    legacy_turn = AsyncMock()
    app.dependency_overrides[get_session_service] = lambda: sessions
    app.dependency_overrides[get_chat_service] = lambda: SimpleNamespace(complete_turn=legacy_turn)
    app.dependency_overrides[log_owned_chat] = lambda: None

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/sessions/owned/turns", json={"content": "hello"})
            assert response.status_code == 410
            assert response.json() == {
                "error": {
                    "code": "turn_endpoint_retired",
                    "message": "Use WebSocket run.start to submit a request.",
                }
            }
            assert (await client.post("/api/sessions/another-user/turns")).status_code == 404
            legacy_turn.assert_not_awaited()

    asyncio.run(exercise())
