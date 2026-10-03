from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from app.domain.models import MessageRole


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunEventType(StrEnum):
    QUEUED = "run.queued"
    STARTED = "run.started"
    CANCELLATION_REQUESTED = "run.cancellation_requested"
    ASSISTANT_DELTA = "assistant.delta"
    MODEL_USAGE = "model.usage"
    MODEL_COMPLETED = "model.completed"
    ASSISTANT_COMPLETED = "assistant.completed"
    COMPLETED = "run.completed"
    FAILED = "run.failed"
    CANCELLED = "run.cancelled"
    INTERRUPTED = "run.interrupted"


_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.INTERRUPTED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.CANCELLING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.INTERRUPTED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.INTERRUPTED: frozenset(),
}


def transition_run(current: RunStatus, next_status: RunStatus) -> RunStatus:
    if next_status not in _TRANSITIONS[current]:
        raise ValueError(f"invalid run transition: {current.value} -> {next_status.value}")
    return next_status


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    provider_id: str
    model_id: str
    adapter_kind: str
    upstream_model_id: str
    messages: tuple[ModelMessage, ...]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    kind: Literal["text_delta", "usage", "completed"]
    text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "text_delta" and self.text is None:
            raise ValueError("text_delta events require text")
        if self.kind == "usage":
            for token_count in (self.input_tokens, self.output_tokens):
                if token_count is not None and token_count < 0:
                    raise ValueError("token counts cannot be negative")


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    sequence: int
    event_type: RunEventType
    event_version: int
    data: dict[str, object]
    created_at: str


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    id: str
    session_id: str
    turn_id: str
    status: RunStatus
    provider_id: str
    model_id: str
    adapter_kind: str
    upstream_model_id: str
    input_message_id: str
    retry_of: str | None
    client_request_id: str
    last_event_sequence: int
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
