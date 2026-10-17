import importlib
import json
from types import ModuleType

import pytest

from app.domain.runtime import RunEventType


def limits() -> ModuleType:
    try:
        return importlib.import_module("app.domain.event_limits")
    except ModuleNotFoundError:
        pytest.fail("durable event storage limits are not implemented")


def test_event_limits_reserve_terminal_capacity_without_exceeding_hard_bounds() -> None:
    module = limits()
    assert module.MAX_RUN_EVENTS == 8192
    assert module.MAX_RUN_EVENT_BYTES == 16 * 1024 * 1024
    assert module.MAX_SINGLE_EVENT_BYTES == 256 * 1024
    assert module.RESERVED_RUN_EVENTS == 16 and module.RESERVED_RUN_EVENT_BYTES == 64 * 1024
    assert (
        module.check_event_storage(RunEventType.MODEL_USAGE, {}, event_count=8175, data_bytes=0)
        == 2
    )
    with pytest.raises(module.EventStorageLimit):
        module.check_event_storage(RunEventType.MODEL_USAGE, {}, event_count=8176, data_bytes=0)
    assert module.check_event_storage(RunEventType.FAILED, {}, event_count=8191, data_bytes=0) == 2
    with pytest.raises(module.EventStorageLimit):
        module.check_event_storage(RunEventType.FAILED, {}, event_count=8192, data_bytes=0)


def test_event_limit_counts_utf8_json_and_checks_single_event_and_byte_reserve() -> None:
    module = limits()
    payload = {"text": "é😀\n", "other": {"count": 2, "flag": True}}
    size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    assert module.event_data_bytes(payload) == size
    assert (
        module.check_event_storage(
            RunEventType.MODEL_USAGE,
            payload,
            event_count=2,
            data_bytes=module.MAX_RUN_EVENT_BYTES - module.RESERVED_RUN_EVENT_BYTES - size,
        )
        == size
    )
    with pytest.raises(module.EventStorageLimit):
        module.check_event_storage(
            RunEventType.MODEL_USAGE,
            payload,
            event_count=2,
            data_bytes=module.MAX_RUN_EVENT_BYTES - module.RESERVED_RUN_EVENT_BYTES - size + 1,
        )
    with pytest.raises(module.EventStorageLimit):
        module.check_event_storage(
            RunEventType.FAILED, {"value": "x" * 262144}, event_count=1, data_bytes=0
        )
    with pytest.raises(module.EventStorageLimit):
        module.check_event_storage(
            RunEventType.FAILED,
            payload,
            event_count=1,
            data_bytes=module.MAX_RUN_EVENT_BYTES - size + 1,
        )


def test_only_terminal_lifecycle_and_final_answer_events_use_reserve() -> None:
    module = limits()
    reserved = {
        RunEventType.CANCELLATION_REQUESTED,
        RunEventType.ASSISTANT_COMPLETED,
        RunEventType.COMPLETED,
        RunEventType.FAILED,
        RunEventType.CANCELLED,
        RunEventType.INTERRUPTED,
    }
    for event_type in RunEventType:
        if event_type in reserved:
            assert module.check_event_storage(event_type, {}, event_count=8176, data_bytes=0) == 2
        else:
            with pytest.raises(module.EventStorageLimit):
                module.check_event_storage(event_type, {}, event_count=8176, data_bytes=0)


def test_event_byte_count_expands_json_numbers_as_postgres_jsonb_does() -> None:
    module = limits()
    payload = {"cost": 3.5e-05, "large": 1e20, "negative_zero": -0.0}
    expected = '{"cost": 0.000035, "large": 100000000000000000000, "negative_zero": 0.0}'
    assert module.event_data_bytes(payload) == len(expected.encode())
