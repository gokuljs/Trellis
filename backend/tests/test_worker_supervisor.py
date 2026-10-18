import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from app.application.errors import ApplicationError
from app.application.worker import WorkerPolicy, WorkerSupervisor
from app.application.worker_ports import WorkerExecutionRepository, WorkerQueuePort
from app.domain.jobs import ExecutionClaim, RunExecutionContext
from app.domain.runtime import RunStatus
from tests.memory_repository import MemoryRepository


class Queue:
    def __init__(self, context: RunExecutionContext) -> None:
        self.context = context
        self.claimed = 0
        self.renewed = 0
        self.released = 0
        self.recovered = 0
        self.lost_response = False
        self.fail_renew = False
        self.fail_begin = False
        self.fail_poll = False
        self.fail_recover = False
        self.fail_release = False
        self.cancel_on_begin = False
        self.startup_finalized = False
        self.attempts: list[tuple[str, str]] = []
        self.stop_after_claim: asyncio.Event | None = None
        self._claim: ExecutionClaim | None = None

    @property
    def claim(self):
        return self._claim

    async def claim_job(self, worker_id, attempt_id, capability):
        self.attempts.append((attempt_id, capability))
        if self._claim is not None:
            if self.lost_response:
                self.lost_response = False
                return self._claim
            return None
        self.claimed += 1
        self._claim = ExecutionClaim(
            "job", self.context.run.id, worker_id, attempt_id, capability, "2099-01-01T00:00:00Z"
        )
        if self.stop_after_claim:
            self.stop_after_claim.set()
        if self.lost_response:
            raise OSError("response lost")
        return self._claim

    async def begin(self, claim):
        if self.fail_begin:
            raise OSError("begin response lost")
        self.context = replace(
            self.context, run=replace(self.context.run, status=RunStatus.RUNNING)
        )
        running = self.context.run
        if self.cancel_on_begin:
            self.context = replace(self.context, run=replace(running, status=RunStatus.CANCELLING))
        return running

    async def renew(self, claim):
        self.renewed += 1
        if self.fail_renew:
            raise OSError("database unavailable")
        return self.context.run

    async def release_unstarted(self, claim):
        self.released += 1
        if self.fail_release:
            raise OSError("release unavailable")

    async def recover_expired(self, limit=100):
        assert limit == 100
        self.recovered += 1
        if self.fail_recover:
            raise OSError("recovery unavailable")
        return 0

    async def get_execution_context(self):
        if self.cancel_on_begin:
            raise PermissionError("Context revoked by cancellation")
        return self.context

    async def transition_run_record(self, run_id, status, event_type, data, **kwargs):
        assert run_id == self.context.run.id
        assert status is RunStatus.CANCELLED
        assert kwargs["stop_reason"] == "user_cancelled"
        self.startup_finalized = True
        self.context = replace(self.context, run=replace(self.context.run, status=status))

    async def get_run(self, run_id):
        if self.fail_poll:
            raise OSError("poll unavailable")
        assert run_id == self.context.run.id
        return self.context.run


class QueueAdapter:
    def __init__(self, queue):
        self.queue = queue

    async def claim(self, worker_id, attempt_id, capability):
        return await self.queue.claim_job(worker_id, attempt_id, capability)

    def __getattr__(self, name):
        return getattr(self.queue, name)


class Execution:
    def __init__(self, queue: Queue) -> None:
        self.queue = queue
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.revoked = False

    async def execute(self, context):
        assert context.run.status is RunStatus.RUNNING
        self.started.set()
        try:
            await self.finished.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    def revoke(self, run_id):
        self.revoked = True


async def setup(tmp_path: Path):
    database = MemoryRepository(tmp_path)
    session = await database.create_session()
    model = (await database.list_models())[0]
    run = await database.create_run(session.id, "turn", "request", "Hello", model)
    queue = Queue(RunExecutionContext("owner", run, session, model))
    execution = Execution(queue)
    policy = WorkerPolicy(
        capacity=1,
        renew_seconds=0.01,
        poll_seconds=0.01,
        recovery_seconds=0.01,
        operation_seconds=0.1,
        max_backoff_seconds=0.01,
        drain_seconds=0.02,
    )
    supervisor = WorkerSupervisor(
        cast(WorkerQueuePort, QueueAdapter(queue)),
        lambda _: cast(WorkerExecutionRepository, queue),
        lambda _: execution,
        policy=policy,
    )
    return queue, execution, supervisor


def test_capacity_is_reserved_and_claim_response_loss_reuses_identity(tmp_path: Path) -> None:
    async def scenario():
        queue, execution, supervisor = await setup(tmp_path)
        queue.lost_response = True
        queue.fail_recover = True
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop))
        await asyncio.wait_for(execution.started.wait(), 1)
        await asyncio.sleep(0.04)
        assert queue.claimed == 1
        assert queue.attempts[0] == queue.attempts[1]
        assert len(queue.attempts) == 2
        assert queue.renewed >= 1 and queue.recovered >= 1
        execution.finished.set()
        stop.set()
        await task
        assert not execution.revoked

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["renew", "poll", "user", "terminal", "shutdown"])
def test_silent_provider_is_cancelled_with_correct_authority(tmp_path: Path, reason: str) -> None:
    async def scenario():
        queue, execution, supervisor = await setup(tmp_path)
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop))
        await asyncio.wait_for(execution.started.wait(), 1)
        if reason == "renew":
            queue.fail_renew = True
        elif reason == "poll":
            queue.fail_poll = True
        elif reason in {"user", "terminal"}:
            status = RunStatus.CANCELLING if reason == "user" else RunStatus.COMPLETED
            queue.context = replace(queue.context, run=replace(queue.context.run, status=status))
        else:
            stop.set()
        await asyncio.wait_for(execution.cancelled.wait(), 1)
        stop.set()
        await task
        assert execution.revoked == (reason in {"renew", "poll", "terminal"})

    asyncio.run(scenario())


def test_shutdown_releases_claim_without_begin(tmp_path: Path) -> None:
    async def scenario():
        queue, execution, supervisor = await setup(tmp_path)
        stop = asyncio.Event()
        queue.stop_after_claim = stop
        await supervisor.run(stop)
        assert queue.released == 1
        assert not execution.started.is_set()

    asyncio.run(scenario())


def test_ambiguous_begin_never_dispatches_provider(tmp_path: Path) -> None:
    async def scenario():
        queue, execution, supervisor = await setup(tmp_path)
        queue.fail_begin = queue.fail_release = True
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop))
        await asyncio.sleep(0.03)
        stop.set()
        await task
        assert queue.released >= 1
        assert not execution.started.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs", [{"capacity": 0}, {"recovery_limit": 101}, {"poll_seconds": 0}, {"renew_seconds": 60}]
)
def test_policy_rejects_unsafe_bounds(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        WorkerPolicy(**kwargs)


@pytest.mark.parametrize("read_denied", [False, True])
def test_cancel_during_startup_finishes_only_with_live_authority(
    tmp_path: Path, read_denied: bool
) -> None:
    async def scenario():
        queue, execution, supervisor = await setup(tmp_path)
        queue.cancel_on_begin = True
        queue.fail_poll = read_denied
        claim = await queue.claim_job("worker", "attempt", "capability")
        if read_denied:
            with pytest.raises(OSError):
                await supervisor._execute_claim(claim)
            assert not queue.startup_finalized
        else:
            await supervisor._execute_claim(claim)
            assert queue.startup_finalized
            assert queue.context.run.status is RunStatus.CANCELLED
        assert not execution.started.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["run_not_owned", "worker_operation_invalid"])
def test_definitively_rejected_claim_gets_fresh_identity(tmp_path: Path, code: str) -> None:
    async def scenario():
        queue, execution, supervisor = await setup(tmp_path)
        identities = []

        class ExpiredResponseQueue(QueueAdapter):
            async def claim(self, worker_id, attempt_id, capability):
                identities.append((attempt_id, capability))
                if len(identities) == 1:
                    raise ApplicationError("database_unavailable", "Committed response lost")
                if len(identities) == 2:
                    raise ApplicationError(code, "Old attempt expired")
                return await self.queue.claim_job(worker_id, attempt_id, capability)

        supervisor._queue = cast(WorkerQueuePort, ExpiredResponseQueue(queue))
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop))
        await asyncio.wait_for(execution.started.wait(), 1)
        execution.finished.set()
        stop.set()
        await task
        assert identities[0] == identities[1]
        assert identities[2] != identities[1]

    asyncio.run(scenario())
