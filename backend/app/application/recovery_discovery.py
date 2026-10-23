import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.application.errors import ApplicationError
from app.domain.runtime import RunSnapshot

logger = logging.getLogger(__name__)

RECOVERY_POLL_SECONDS = 5.0
RECOVERY_BATCH_SIZE = 20


class RecoveryCandidates(Protocol):
    async def list_recovery_candidates(self, installation_id: str) -> list[RunSnapshot]: ...


class RecoveryRuntime(Protocol):
    async def recover_candidate(self, run: RunSnapshot) -> RunSnapshot: ...


class RecoveryDiscovery:
    def __init__(
        self,
        repository: RecoveryCandidates,
        runtime: RecoveryRuntime,
        installation_id: str,
        authentication_valid: Callable[[], bool],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("Discovery request timeout must be positive")
        self._repository = repository
        self._runtime = runtime
        self._installation_id = installation_id
        self._authentication_valid = authentication_valid
        self._sleep = sleep
        self._request_timeout_seconds = request_timeout_seconds
        self._task: asyncio.Task[None] | None = None
        self._activation = 0
        self._closed = False

    def activate(self, *, authentication_revision: int | None = None) -> None:
        if self._closed:
            return
        self._activation = (
            self._activation + 1 if authentication_revision is None else authentication_revision
        )
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._discover(),
                name="trellis-account-recovery",
                context=contextvars.Context(),
            )
            self._task.add_done_callback(self._completed, context=contextvars.Context())

    def _completed(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._log_failure("recovery_discovery_unavailable")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._task is not None:
                self._task.cancel()
        if self._task is not None:
            await asyncio.shield(asyncio.gather(self._task, return_exceptions=True))

    async def _discover(self) -> None:
        while not self._closed and self._authentication_valid():
            activation = self._activation
            try:
                async with asyncio.timeout(self._request_timeout_seconds):
                    candidates = await self._repository.list_recovery_candidates(
                        self._installation_id
                    )
                for run in candidates[:RECOVERY_BATCH_SIZE]:
                    if self._closed or not self._authentication_valid():
                        return
                    try:
                        await self._runtime.recover_candidate(run)
                    except ApplicationError as error:
                        if error.code in {"run_capacity", "runtime_shutting_down"}:
                            break
                        raise
            except ApplicationError as error:
                if error.code == "cloud_access_denied":
                    if activation != self._activation and self._authentication_valid():
                        continue
                    return
                self._log_failure(error.code)
            except TimeoutError, RuntimeError, ValueError:
                self._log_failure("recovery_discovery_unavailable")
            await self._sleep(RECOVERY_POLL_SECONDS)

    @staticmethod
    def _log_failure(code: str) -> None:
        logger.warning(
            "run.recovery.discovery_deferred",
            extra={
                "event": "run.recovery.discovery_deferred",
                "error_code": code,
                "outcome": "interrupted",
            },
        )
