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


class ModelCallStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}
    ),
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

_TRANSITION_EVENTS: dict[tuple[RunStatus, RunStatus], RunEventType] = {
    (RunStatus.QUEUED, RunStatus.RUNNING): RunEventType.STARTED,
    (RunStatus.QUEUED, RunStatus.FAILED): RunEventType.FAILED,
    (RunStatus.QUEUED, RunStatus.CANCELLED): RunEventType.CANCELLED,
    (RunStatus.QUEUED, RunStatus.INTERRUPTED): RunEventType.INTERRUPTED,
    (RunStatus.RUNNING, RunStatus.CANCELLING): RunEventType.CANCELLATION_REQUESTED,
    (RunStatus.RUNNING, RunStatus.COMPLETED): RunEventType.COMPLETED,
    (RunStatus.RUNNING, RunStatus.FAILED): RunEventType.FAILED,
    (RunStatus.RUNNING, RunStatus.CANCELLED): RunEventType.CANCELLED,
    (RunStatus.RUNNING, RunStatus.INTERRUPTED): RunEventType.INTERRUPTED,
    (RunStatus.CANCELLING, RunStatus.CANCELLED): RunEventType.CANCELLED,
    (RunStatus.CANCELLING, RunStatus.FAILED): RunEventType.FAILED,
    (RunStatus.CANCELLING, RunStatus.INTERRUPTED): RunEventType.INTERRUPTED,
}
_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        RunEventType.QUEUED,
        RunEventType.STARTED,
        RunEventType.CANCELLATION_REQUESTED,
        RunEventType.COMPLETED,
        RunEventType.FAILED,
        RunEventType.CANCELLED,
        RunEventType.INTERRUPTED,
    }
)
_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }
)


def transition_run(current: RunStatus, next_status: RunStatus) -> RunStatus:
    if next_status not in _TRANSITIONS[current]:
        raise ValueError(f"invalid run transition: {current.value} -> {next_status.value}")
    return next_status


def validate_run_event_transition(
    current: RunStatus,
    next_status: RunStatus,
    event_type: RunEventType,
) -> None:
    transition_run(current, next_status)
    if _TRANSITION_EVENTS[(current, next_status)] is not event_type:
        raise ValueError("run event does not match status transition")


def is_lifecycle_event(event_type: RunEventType) -> bool:
    return event_type in _LIFECYCLE_EVENT_TYPES


def is_terminal_run_status(status: RunStatus) -> bool:
    return status in _TERMINAL_RUN_STATUSES


def transition_model_call(
    current: ModelCallStatus, next_status: ModelCallStatus
) -> ModelCallStatus:
    valid_transitions = {
        ModelCallStatus.PENDING: {
            ModelCallStatus.STREAMING,
            ModelCallStatus.COMPLETED,
            ModelCallStatus.FAILED,
            ModelCallStatus.CANCELLED,
            ModelCallStatus.TIMED_OUT,
        },
        ModelCallStatus.STREAMING: {
            ModelCallStatus.STREAMING,
            ModelCallStatus.COMPLETED,
            ModelCallStatus.FAILED,
            ModelCallStatus.CANCELLED,
            ModelCallStatus.TIMED_OUT,
        },
    }
    if next_status not in valid_transitions.get(current, set()):
        raise ValueError(f"invalid model call transition: {current.value} -> {next_status.value}")
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
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    finish_reason: str | None = None
    provider_response_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "text_delta" and self.text is None:
            raise ValueError("text_delta events require text")
        if self.kind == "usage":
            for token_count in (
                self.input_tokens,
                self.output_tokens,
                self.reasoning_tokens,
                self.cached_tokens,
            ):
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
    max_model_calls: int
    max_tool_calls: int
    deadline_at: str
    cancel_requested_at: str | None
    lease_expires_at: str | None
    recovery_count: int
    stop_reason: str | None
    last_event_sequence: int
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    id: str
    run_id: str
    step_index: int
    provider_id: str
    model_id: str
    adapter_kind: str
    status: ModelCallStatus
    request_snapshot: dict[str, object]
    response_snapshot: dict[str, object] | None
    provider_response_id: str | None
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    estimated_cost: float | None
    error_code: str | None
    error_message: str | None
    started_at: str
    finished_at: str | None
