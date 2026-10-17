from dataclasses import dataclass
from enum import StrEnum

from app.domain.models import ModelDescriptor, Session
from app.domain.runtime import RunSnapshot


class JobStatus(StrEnum):
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    job_id: str
    run_id: str
    worker_id: str
    attempt_id: str
    capability: str
    lease_expires_at: str


@dataclass(frozen=True, slots=True)
class RunExecutionContext:
    owner_id: str
    run: RunSnapshot
    session: Session
    model: ModelDescriptor
