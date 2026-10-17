from typing import Protocol

from app.application.ports import ContextCheckpointRepository, RunRepository, SessionRepository
from app.domain.jobs import ExecutionClaim, RunExecutionContext
from app.domain.runtime import RunSnapshot


class WorkerQueuePort(Protocol):
    async def claim(
        self, worker_id: str, attempt_id: str, capability: str
    ) -> ExecutionClaim | None: ...

    async def begin(self, claim: ExecutionClaim) -> RunSnapshot: ...

    async def renew(self, claim: ExecutionClaim) -> RunSnapshot: ...

    async def release_unstarted(self, claim: ExecutionClaim) -> None: ...

    async def recover_expired(self, limit: int = 100) -> int: ...


class WorkerExecutionRepository(
    RunRepository, ContextCheckpointRepository, SessionRepository, Protocol
):
    @property
    def claim(self) -> ExecutionClaim: ...

    async def get_execution_context(self) -> RunExecutionContext: ...
