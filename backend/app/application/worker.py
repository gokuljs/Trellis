import asyncio
import logging
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4

from app.application.worker_ports import WorkerExecutionRepository, WorkerQueuePort
from app.domain.jobs import ExecutionClaim, RunExecutionContext
from app.domain.runtime import RunStatus, is_terminal_run_status

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerPolicy:
    capacity: int = 4
    renew_seconds: float = 15
    poll_seconds: float = 1
    max_backoff_seconds: float = 5
    recovery_seconds: float = 5
    recovery_limit: int = 100
    operation_seconds: float = 5
    drain_seconds: float = 30

    def __post_init__(self) -> None:
        if self.capacity < 1 or not 1 <= self.recovery_limit <= 100:
            raise ValueError("Worker capacity and recovery batch must be positive and bounded")
        if (
            min(
                self.renew_seconds,
                self.poll_seconds,
                self.max_backoff_seconds,
                self.recovery_seconds,
                self.operation_seconds,
                self.drain_seconds,
            )
            <= 0
            or self.renew_seconds + self.operation_seconds >= 60
        ):
            raise ValueError("Worker intervals must be positive and fit within the lease")


class ExecutionPort(Protocol):
    async def execute(self, context: RunExecutionContext) -> None: ...

    def revoke(self, run_id: str) -> None: ...


RepositoryFactory = Callable[[ExecutionClaim], WorkerExecutionRepository]
ExecutorFactory = Callable[[WorkerExecutionRepository], ExecutionPort]


async def _delay(stop: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await stop.wait()


class WorkerSupervisor:
    def __init__(
        self,
        queue: WorkerQueuePort,
        repository_factory: RepositoryFactory,
        executor_factory: ExecutorFactory,
        *,
        worker_id: str | None = None,
        policy: WorkerPolicy | None = None,
    ) -> None:
        self.worker_id = worker_id or str(uuid4())
        self._queue = queue
        self._repository_factory = repository_factory
        self._executor_factory = executor_factory
        self._policy = policy or WorkerPolicy()

    async def run(self, stop: asyncio.Event) -> None:
        slots = [asyncio.create_task(self._slot(stop)) for _ in range(self._policy.capacity)]
        recovery = asyncio.create_task(self._recover(stop))
        try:
            await stop.wait()
        finally:
            stop.set()
            recovery.cancel()
            with suppress(asyncio.CancelledError):
                await recovery
            _, pending = await asyncio.wait(slots, timeout=self._policy.drain_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*slots, return_exceptions=True)

    async def _slot(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            attempt_id, capability = str(uuid4()), secrets.token_urlsafe(32)
            claim = None
            backoff = self._policy.poll_seconds
            # Reuse this identity after an ambiguous claim response.
            while not stop.is_set():
                try:
                    async with asyncio.timeout(self._policy.operation_seconds):
                        claim = await self._queue.claim(self.worker_id, attempt_id, capability)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Worker claim failed; retrying the same attempt")
                    await _delay(stop, backoff)
                    backoff = min(backoff * 2, self._policy.max_backoff_seconds)
            if claim is None:
                await _delay(stop, self._policy.poll_seconds)
                continue
            try:
                if stop.is_set():
                    await self._release(claim)
                    return
                await self._execute_claim(claim)
            except asyncio.CancelledError:
                await self._release(claim)
                raise
            except Exception:
                logger.exception("Worker attempt stopped", exc_info=False)
                await self._release(claim)
                await _delay(stop, self._policy.max_backoff_seconds)

    async def _release(self, claim: ExecutionClaim) -> None:
        try:
            async with asyncio.timeout(self._policy.operation_seconds):
                await self._queue.release_unstarted(claim)
        except Exception:
            # A started or expired attempt is reconciled by durable recovery.
            logger.warning("Could not release unstarted worker assignment")

    async def _execute_claim(self, claim: ExecutionClaim) -> None:
        async with asyncio.timeout(self._policy.operation_seconds):
            running = await self._queue.begin(claim)
        if running.status is not RunStatus.RUNNING:
            return
        repository = self._repository_factory(claim)
        async with asyncio.timeout(self._policy.operation_seconds):
            context = await repository.get_execution_context()
        context = replace(context, run=running)
        executor = self._executor_factory(repository)
        execution = asyncio.create_task(executor.execute(context))
        renew = asyncio.create_task(self._renew(claim, executor, execution))
        cancellation = asyncio.create_task(self._poll(repository, executor, execution))
        try:
            await execution
        except asyncio.CancelledError:
            # User cancellation cancels the child, not the capacity slot.
            if (current := asyncio.current_task()) is not None and current.cancelling():
                raise
        finally:
            renew.cancel()
            cancellation.cancel()
            await asyncio.gather(renew, cancellation, return_exceptions=True)

    async def _renew(
        self, claim: ExecutionClaim, executor: ExecutionPort, execution: asyncio.Task[None]
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._policy.renew_seconds)
                async with asyncio.timeout(self._policy.operation_seconds):
                    run = await self._queue.renew(claim)
                if is_terminal_run_status(run.status):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Stopping execution after worker renewal failed")
        executor.revoke(claim.run_id)
        execution.cancel()

    async def _poll(
        self,
        repository: WorkerExecutionRepository,
        executor: ExecutionPort,
        execution: asyncio.Task[None],
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._policy.poll_seconds)
                async with asyncio.timeout(self._policy.operation_seconds):
                    run = await repository.get_run(repository.claim.run_id)
                if run is not None and run.status is RunStatus.CANCELLING:
                    execution.cancel()
                    return
                if run is None or is_terminal_run_status(run.status):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Stopping execution after assignment verification failed")
        executor.revoke(repository.claim.run_id)
        execution.cancel()

    async def _recover(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with asyncio.timeout(self._policy.operation_seconds):
                    await self._queue.recover_expired(self._policy.recovery_limit)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Worker expiry sweep failed")
            await _delay(stop, self._policy.recovery_seconds)
