import asyncio
import json
from dataclasses import asdict

from app.domain.runtime import RunEvent

DEFAULT_MAX_PENDING_BYTES = 512 * 1024


class RuntimeEventSubscription:
    def __init__(
        self, run_id: str, capacity: int, max_pending_bytes: int = DEFAULT_MAX_PENDING_BYTES
    ) -> None:
        self.run_id = run_id
        self._queue: asyncio.Queue[tuple[RunEvent, int] | None] = asyncio.Queue(maxsize=capacity)
        self._max_pending_bytes = max_pending_bytes
        self._pending_bytes = 0
        self._closed = False

    async def receive(self) -> RunEvent | None:
        pending = await self._queue.get()
        if pending is None:
            return None
        event, size = pending
        self._pending_bytes -= size
        return event

    def _publish(self, event: RunEvent, size: int) -> bool:
        if self._closed:
            return False
        if self._pending_bytes + size > self._max_pending_bytes:
            self._terminate()
            return False
        try:
            self._queue.put_nowait((event, size))
        except asyncio.QueueFull:
            self._terminate()
            return False
        self._pending_bytes += size
        return True

    def _terminate(self) -> None:
        self._closed = True
        while not self._queue.empty():
            self._queue.get_nowait()
        self._pending_bytes = 0
        self._queue.put_nowait(None)


class RuntimeEventHub:
    """Live fanout with bounded queues; persisted events remain the source of truth.

    Each subscriber retains at most 256 events and 512 KiB of serialized UTF-8
    event data by default. Eight socket subscriptions therefore retain at most
    4 MiB of queued event data, independently of a slow socket's send operation.
    """

    def __init__(
        self, *, max_pending_events: int = 256, max_pending_bytes: int = DEFAULT_MAX_PENDING_BYTES
    ) -> None:
        if max_pending_events < 1:
            raise ValueError("event subscription capacity must be positive")
        if max_pending_bytes < 1:
            raise ValueError("event subscription byte budget must be positive")
        self._max_pending_events = max_pending_events
        self._max_pending_bytes = max_pending_bytes
        self._subscribers: dict[str, set[RuntimeEventSubscription]] = {}

    def subscribe(self, run_id: str) -> RuntimeEventSubscription:
        subscription = RuntimeEventSubscription(
            run_id, self._max_pending_events, self._max_pending_bytes
        )
        self._subscribers.setdefault(run_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: RuntimeEventSubscription) -> None:
        subscription._terminate()
        subscribers = self._subscribers.get(subscription.run_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            del self._subscribers[subscription.run_id]

    async def publish(self, event: RunEvent) -> None:
        size = len(json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":")).encode())
        for subscription in tuple(self._subscribers.get(event.run_id, ())):
            if not subscription._publish(event, size):
                self.unsubscribe(subscription)
