import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import uuid4

import pytest
from run_fixtures import run_record

from app.application.errors import ApplicationError
from app.application.recovery_discovery import RecoveryDiscovery
from app.domain.recovery import RECOVERY_RUNTIME_VERSION, RecoveryContract
from app.domain.runtime import RunSnapshot


class PollClock:
    def __init__(self) -> None:
        self.intervals: asyncio.Queue[float] = asyncio.Queue()
        self.ticks: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, seconds: float) -> None:
        self.intervals.put_nowait(seconds)
        await self.ticks.get()

    async def waiting(self) -> None:
        assert await asyncio.wait_for(self.intervals.get(), 1) == 5


class Candidates:
    def __init__(self, runs: list[RunSnapshot] | None = None) -> None:
        self.runs = runs or []
        self.queries: asyncio.Queue[str] = asyncio.Queue()
        self.error: Exception | None = None
        self.operation: Callable[[], Awaitable[object]] | None = None

    async def list_recovery_candidates(self, installation_id: str) -> list[RunSnapshot]:
        self.queries.put_nowait(installation_id)
        if self.operation is not None:
            await self.operation()
        if self.error is not None:
            raise self.error
        return self.runs


class Runtime:
    def __init__(self) -> None:
        self.runs: list[RunSnapshot] = []
        self.error: ApplicationError | None = None
        self.operation: Callable[[], Awaitable[object]] | None = None

    async def recover_candidate(self, run: RunSnapshot) -> RunSnapshot:
        if self.error is not None:
            raise self.error
        self.runs.append(run)
        if self.operation is not None:
            await self.operation()
        return run


def candidates(count: int) -> list[RunSnapshot]:
    record = run_record()
    return [
        replace(
            record,
            id=str(uuid4()),
            recovery_contract=RecoveryContract(
                1, RECOVERY_RUNTIME_VERSION, (), (), record.client_request_id, str(uuid4()), None
            ),
        )
        for _ in range(count)
    ]


def test_activation_queries_immediately_then_in_bounded_batches_every_five_seconds():
    async def exercise():
        repository, runtime, clock = Candidates(candidates(21)), Runtime(), PollClock()
        discovery = RecoveryDiscovery(
            repository, runtime, "installation", lambda: True, sleep=clock.sleep
        )
        discovery.activate()
        await clock.waiting()
        assert await repository.queries.get() == "installation"
        assert runtime.runs == repository.runs[:20]
        discovery.activate()
        discovery.activate()
        await asyncio.sleep(0)
        assert repository.queries.empty()
        clock.ticks.put_nowait(None)
        await clock.waiting()
        assert await repository.queries.get() == "installation"
        assert runtime.runs == repository.runs[:20] * 2
        await discovery.close()
        discovery.activate()
        await asyncio.sleep(0)
        assert repository.queries.empty()

    asyncio.run(exercise())


def test_no_discovery_before_activation_or_after_authentication_expires():
    async def exercise():
        valid = False
        repository, runtime, clock = Candidates(candidates(2)), Runtime(), PollClock()
        discovery = RecoveryDiscovery(
            repository, runtime, "installation", lambda: valid, sleep=clock.sleep
        )
        await asyncio.sleep(0)
        assert repository.queries.empty()
        discovery.activate()
        await asyncio.sleep(0)
        assert repository.queries.empty()
        valid = True
        discovery.activate()
        await clock.waiting()
        assert len(runtime.runs) == 2
        valid = False
        clock.ticks.put_nowait(None)
        await asyncio.sleep(0)
        assert repository.queries.qsize() == 1
        valid = True
        discovery.activate()
        await clock.waiting()
        assert repository.queries.qsize() == 2
        await discovery.close()

    asyncio.run(exercise())


def test_expiring_authentication_stops_between_candidates():
    async def exercise():
        valid = True

        async def expire():
            nonlocal valid
            valid = False

        repository, runtime = Candidates(candidates(2)), Runtime()
        runtime.operation = expire
        discovery = RecoveryDiscovery(repository, runtime, "installation", lambda: valid)
        discovery.activate()
        await asyncio.wait_for(repository.queries.get(), 1)
        await asyncio.sleep(0)
        assert runtime.runs == repository.runs[:1]
        await discovery.close()

    asyncio.run(exercise())


def test_rejected_authentication_waits_for_a_new_verified_activation():
    async def exercise():
        repository, runtime, clock = Candidates(candidates(1)), Runtime(), PollClock()
        repository.error = ApplicationError("cloud_access_denied", "expired")
        discovery = RecoveryDiscovery(
            repository, runtime, "installation", lambda: True, sleep=clock.sleep
        )
        discovery.activate()
        await asyncio.wait_for(repository.queries.get(), 1)
        await asyncio.sleep(0)
        assert runtime.runs == []
        assert clock.intervals.empty()
        repository.error = None
        discovery.activate()
        await clock.waiting()
        assert runtime.runs == repository.runs
        await discovery.close()

    asyncio.run(exercise())


def test_verification_during_a_rejected_query_is_not_lost():
    async def exercise():
        entered, released = asyncio.Event(), asyncio.Event()
        repository, runtime, clock = Candidates(candidates(1)), Runtime(), PollClock()
        initial = True

        async def reject_first():
            nonlocal initial
            if initial:
                initial = False
                entered.set()
                await released.wait()
                raise ApplicationError("cloud_access_denied", "expired")

        repository.operation = reject_first
        discovery = RecoveryDiscovery(
            repository, runtime, "installation", lambda: True, sleep=clock.sleep
        )
        discovery.activate()
        await asyncio.wait_for(entered.wait(), 1)
        discovery.activate()
        released.set()
        await clock.waiting()
        assert repository.queries.qsize() == 2
        assert runtime.runs == repository.runs
        await discovery.close()

    asyncio.run(exercise())


def test_same_verified_token_during_a_rejected_query_does_not_repeat_discovery():
    async def exercise():
        entered, released = asyncio.Event(), asyncio.Event()
        repository, runtime, clock = Candidates(), Runtime(), PollClock()

        async def reject():
            entered.set()
            await released.wait()
            raise ApplicationError("cloud_access_denied", "expired")

        repository.operation = reject
        discovery = RecoveryDiscovery(
            repository, runtime, "installation", lambda: True, sleep=clock.sleep
        )
        discovery.activate(authentication_revision=1)
        await asyncio.wait_for(entered.wait(), 1)
        discovery.activate(authentication_revision=1)
        discovery.activate(authentication_revision=1)
        released.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert repository.queries.qsize() == 1
        assert clock.intervals.empty()
        await discovery.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("error_code", ["run_capacity", "runtime_shutting_down"])
def test_capacity_or_shutdown_does_not_claim_additional_candidates(error_code):
    async def exercise():
        repository, runtime, clock = Candidates(candidates(3)), Runtime(), PollClock()
        runtime.error = ApplicationError(error_code, "busy")
        discovery = RecoveryDiscovery(
            repository, runtime, "installation", lambda: True, sleep=clock.sleep
        )
        discovery.activate()
        await clock.waiting()
        assert runtime.runs == []
        runtime.error = None
        clock.ticks.put_nowait(None)
        await clock.waiting()
        assert runtime.runs == repository.runs
        await discovery.close()

    asyncio.run(exercise())


def test_cloud_failure_and_timeout_wait_for_the_next_poll():
    async def exercise():
        repository, runtime, clock = Candidates(candidates(1)), Runtime(), PollClock()
        repository.error = ApplicationError("database_unavailable", "offline")
        discovery = RecoveryDiscovery(
            repository,
            runtime,
            "installation",
            lambda: True,
            sleep=clock.sleep,
            request_timeout_seconds=0.01,
        )
        discovery.activate()
        await clock.waiting()
        assert repository.queries.qsize() == 1
        repository.error = None
        repository.operation = asyncio.Event().wait
        clock.ticks.put_nowait(None)
        await clock.waiting()
        assert repository.queries.qsize() == 2
        repository.operation = None
        clock.ticks.put_nowait(None)
        await clock.waiting()
        assert runtime.runs == repository.runs
        await discovery.close()

    asyncio.run(exercise())


def test_monitor_cancellation_drains_query_and_does_not_inherit_request_context():
    request_context = contextvars.ContextVar("request_context", default="empty")

    async def exercise():
        entered, drained = asyncio.Event(), asyncio.Event()

        async def query():
            assert request_context.get() == "empty"
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                drained.set()

        repository, runtime = Candidates(), Runtime()
        repository.operation = query
        discovery = RecoveryDiscovery(repository, runtime, "installation", lambda: True)
        request_context.set("private-request")
        discovery.activate()
        await asyncio.wait_for(entered.wait(), 1)
        await discovery.close()
        assert drained.is_set()
        await discovery.close()
        assert runtime.runs == []

    asyncio.run(exercise())


def test_concurrent_close_and_caller_cancellation_preserve_query_draining():
    async def exercise():
        entered, draining, released, drained = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def query():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                draining.set()
                await released.wait()
                drained.set()

        repository, runtime = Candidates(), Runtime()
        repository.operation = query
        discovery = RecoveryDiscovery(repository, runtime, "installation", lambda: True)
        discovery.activate()
        await asyncio.wait_for(entered.wait(), 1)
        first = asyncio.create_task(discovery.close())
        await asyncio.wait_for(draining.wait(), 1)
        second = asyncio.create_task(discovery.close())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0)
        assert not second.done()
        released.set()
        await second
        assert drained.is_set()

    asyncio.run(exercise())
