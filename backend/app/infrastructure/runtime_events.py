import asyncio

from app.domain.runtime import RunEvent


class RuntimeEventSubscription:
    def __init__(self, run_id: str, capacity: int) -> None:
        self.run_id = run_id
        self._queue: asyncio.Queue[RunEvent | None] = asyncio.Queue(maxsize=capacity)

    async def receive(self) -> RunEvent | None:
        return await self._queue.get()

    def _publish(self, event: RunEvent) -> bool:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._terminate()
            return False
        return True

    def _terminate(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


class RuntimeEventHub:
    """In-process live fanout; persisted events remain the source of truth."""

    def __init__(self, *, max_pending_events: int = 256) -> None:
        if max_pending_events < 1:
            raise ValueError("event subscription capacity must be positive")
        self._max_pending_events = max_pending_events
        self._subscribers: dict[str, set[RuntimeEventSubscription]] = {}

    def subscribe(self, run_id: str) -> RuntimeEventSubscription:
        subscription = RuntimeEventSubscription(run_id, self._max_pending_events)
        self._subscribers.setdefault(run_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: RuntimeEventSubscription) -> None:
        subscribers = self._subscribers.get(subscription.run_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            del self._subscribers[subscription.run_id]

    async def publish(self, event: RunEvent) -> None:
        for subscription in tuple(self._subscribers.get(event.run_id, ())):
            if not subscription._publish(event):
                self.unsubscribe(subscription)
