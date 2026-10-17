import asyncio

from app.domain.runtime import RunEvent, RunEventType
from app.infrastructure.runtime_events import RuntimeEventHub


def _event(sequence: int, text: str) -> RunEvent:
    return RunEvent("run-1", sequence, RunEventType.ASSISTANT_DELTA, 1, {"text": text}, "created")


def test_event_hub_disconnects_only_subscribers_over_the_byte_budget() -> None:
    async def exercise() -> None:
        hub = RuntimeEventHub()
        slow = hub.subscribe("run-1")
        fast = hub.subscribe("run-1")
        first = _event(1, "a" * 300_000)
        second = _event(2, "b" * 300_000)
        await hub.publish(first)
        assert await fast.receive() == first
        await hub.publish(second)
        assert await slow.receive() is None
        assert await fast.receive() == second
        assert hub._subscribers == {"run-1": {fast}}
        hub.unsubscribe(fast)

    asyncio.run(exercise())


def test_event_hub_bounds_utf8_bytes_for_one_large_event() -> None:
    async def exercise() -> None:
        hub = RuntimeEventHub()
        subscription = hub.subscribe("run-1")
        await hub.publish(_event(1, "é" * 270_000))
        assert await subscription.receive() is None
        assert hub._subscribers == {}

    asyncio.run(exercise())
