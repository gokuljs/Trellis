from dataclasses import dataclass
from typing import Literal

MAX_ASSISTANT_BATCH_BYTES = 4096
MAX_ASSISTANT_OUTPUT_BYTES = 256_000
OutputStatus = Literal["streaming", "complete", "partial"]


@dataclass(frozen=True, slots=True)
class AssistantOutput:
    id: str
    session_id: str
    turn_id: str
    run_id: str
    model_call_id: str
    step_index: int
    content: str
    status: OutputStatus
    next_batch_index: int
    stop_reason: str | None
    promoted_message_id: str | None
    created_at: str


def validate_assistant_batch(batch_index: int, text: str) -> None:
    if batch_index < 0 or not text or len(text.encode("utf-8")) > MAX_ASSISTANT_BATCH_BYTES:
        raise ValueError("invalid assistant batch")
