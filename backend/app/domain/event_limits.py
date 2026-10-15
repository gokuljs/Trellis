"""Durable run replay limits; retained events are never evicted to make room."""

import json
from decimal import Decimal

from app.domain.runtime import RunEventType

MAX_RUN_EVENTS = 8192
MAX_RUN_EVENT_BYTES = 16 * 1024 * 1024
MAX_SINGLE_EVENT_BYTES = 256 * 1024
RESERVED_RUN_EVENTS = 16
RESERVED_RUN_EVENT_BYTES = 64 * 1024
EVENT_STORAGE_LIMIT_MESSAGE = "This run reached its stored-event limit. Start a new run."
_RESERVED_EVENTS = frozenset(
    {
        RunEventType.CANCELLATION_REQUESTED,
        RunEventType.ASSISTANT_COMPLETED,
        RunEventType.COMPLETED,
        RunEventType.FAILED,
        RunEventType.CANCELLED,
        RunEventType.INTERRUPTED,
    }
)


class EventStorageLimit(Exception):
    def __init__(self) -> None:
        super().__init__("event_storage_limit")


def event_data_bytes(data: dict[str, object]) -> int:
    """UTF-8 JSON payload bytes, matching PostgreSQL jsonb text spacing."""
    normalized = json.loads(
        json.dumps(data, ensure_ascii=False, allow_nan=False), parse_float=Decimal
    )
    return _jsonb_bytes(normalized)


def _jsonb_bytes(value: object) -> int:
    if isinstance(value, dict):
        return (
            2
            + 2 * max(0, len(value) - 1)
            + sum(_jsonb_bytes(key) + 2 + _jsonb_bytes(item) for key, item in value.items())
        )
    if isinstance(value, list):
        return 2 + 2 * max(0, len(value) - 1) + sum(_jsonb_bytes(item) for item in value)
    if isinstance(value, Decimal):
        # jsonb stores numeric exponents in decimal notation and normalizes -0.
        return len(format(value.copy_abs() if value.is_zero() else value, "f"))
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def check_event_storage(
    event_type: RunEventType,
    data: dict[str, object],
    *,
    event_count: int,
    data_bytes: int,
) -> int:
    size = event_data_bytes(data)
    reserved = event_type in _RESERVED_EVENTS
    count_limit = MAX_RUN_EVENTS - (0 if reserved else RESERVED_RUN_EVENTS)
    byte_limit = MAX_RUN_EVENT_BYTES - (0 if reserved else RESERVED_RUN_EVENT_BYTES)
    if (
        size > MAX_SINGLE_EVENT_BYTES
        or event_count + 1 > count_limit
        or data_bytes + size > byte_limit
    ):
        raise EventStorageLimit
    return size
