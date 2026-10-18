import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from starlette.types import Scope

from app.domain.models import Session

logger = logging.getLogger(__name__)
_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"}
)
_OWNED_CHAT_ID = "trellis.logging.owned_chat_id"


@contextmanager
def owned_chat_logging(scope: Scope, session: Session) -> Iterator[None]:
    # Call only after the account repository has established session ownership.
    scope[_OWNED_CHAT_ID] = session.id
    yield


class RequestLoggingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path") in {"/health", "/ready"}:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        method = method if method in _HTTP_METHODS else "_OTHER"
        started = time.monotonic()
        status_code: int | None = None
        outcome = "failed"

        async def observed_send(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
            outcome = (
                "failed"
                if status_code is None or status_code >= 500
                else "rejected"
                if status_code >= 400
                else "completed"
            )
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            metadata = {
                "event": "http.request.completed",
                "route": route,
                "method": method,
                "status_code": status_code,
                "duration_ms": (time.monotonic() - started) * 1000,
                "outcome": outcome,
            }
            chat_id = scope.get(_OWNED_CHAT_ID)
            if isinstance(chat_id, str):
                metadata["chat_id"] = chat_id
            if outcome == "failed":
                logger.error("http.request.completed", extra=metadata)
            elif outcome == "rejected":
                logger.warning("http.request.completed", extra=metadata)
            else:
                logger.info("http.request.completed", extra=metadata)
