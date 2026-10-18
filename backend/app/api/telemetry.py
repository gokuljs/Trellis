import asyncio
import re
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.application.telemetry import (
    NO_TELEMETRY,
    Attribute,
    Attributes,
    Operation,
    Telemetry,
    TraceLink,
    run_identifiers,
)
from app.domain.models import Session
from app.domain.runtime import RunSnapshot

_TRACEPARENT = re.compile(rb"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"}
)
_RPC_METHODS = frozenset(
    {"auth.authenticate", "run.start", "run.resume", "run.cancel", "run.respond"}
)
_CHAT_CORRELATION = "trellis.telemetry.chat"


def transport_telemetry(scope: Scope) -> Telemetry:
    return cast(Telemetry, getattr(scope["app"].state, "telemetry", NO_TELEMETRY))


@contextmanager
def owned_chat_telemetry(scope: Scope, session: Session) -> Iterator[None]:
    identifiers = {"chatId": session.id}
    scope[_CHAT_CORRELATION] = identifiers
    with transport_telemetry(scope).correlate(identifiers):
        yield


def _trace_parent(scope: Scope) -> TraceLink | None:
    values = [value for key, value in scope.get("headers", ()) if key.lower() == b"traceparent"]
    if len(values) != 1 or len(values[0]) != 55:
        return None
    match = _TRACEPARENT.fullmatch(values[0])
    if match is None:
        return None
    trace_id, span_id, flags = (int(value, 16) for value in match.groups())
    if not trace_id or not span_id:
        return None
    return TraceLink(trace_id, span_id, sampled=bool(flags & 1), remote=True)


class TransportTelemetryMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in {"/health", "/ready"}:
            await self.app(scope, receive, send)
            return
        telemetry = transport_telemetry(scope)
        method = scope.get("method", "")
        method = method if method in _HTTP_METHODS else "_OTHER"
        started = time.monotonic()
        status = 500
        outcome = "error"

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with telemetry.operation(
            "http.request",
            {"http.request.method": method},
            root=True,
            parent=_trace_parent(scope),
            kind="server",
        ) as operation:
            try:
                await self.app(scope, receive, observed_send)
                outcome = (
                    "error" if status >= 500 else "client_error" if status >= 400 else "completed"
                )
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            finally:
                # The router supplies its declared template after matching. Never use scope["path"].
                route = getattr(scope.get("route"), "path", "unmatched")
                attributes: dict[str, Attribute] = {
                    "http.request.method": method,
                    "http.route": route,
                    "http.response.status_code": status,
                    "outcome": outcome,
                    **scope.get(_CHAT_CORRELATION, {}),
                }
                operation.set_attributes(attributes)
                operation.outcome(outcome)
                elapsed = time.monotonic() - started
                telemetry.count("trellis.http.requests", attributes=attributes)
                telemetry.observe("trellis.http.duration", elapsed, attributes)
                telemetry.event("http.request.finished", attributes, error=outcome == "error")


@dataclass(slots=True)
class CommandObservation:
    operation: Operation
    telemetry: Telemetry
    scopes: ExitStack
    outcome_name: str = "completed"
    error_code: str | None = None
    attributes: dict[str, Attribute] = field(default_factory=dict)

    def outcome(self, name: str, error_code: str | None = None) -> None:
        self.outcome_name = name
        self.error_code = error_code

    def set_attributes(self, attributes: Attributes) -> None:
        self.attributes.update(attributes)
        self.operation.set_attributes(attributes)

    def correlate(self, run: RunSnapshot) -> None:
        identifiers = run_identifiers(run)
        self.scopes.enter_context(self.telemetry.correlate(identifiers))
        self.set_attributes(identifiers)


@contextmanager
def runtime_command(telemetry: Telemetry, method: str) -> Iterator[CommandObservation]:
    safe_method = method if method in _RPC_METHODS else "unknown"
    started = time.monotonic()
    with (
        telemetry.operation(
            "runtime.command", {"rpc.method": safe_method}, root=True, kind="server"
        ) as operation,
        ExitStack() as scopes,
    ):
        command = CommandObservation(operation, telemetry, scopes)
        try:
            yield command
        except asyncio.CancelledError:
            command.outcome("cancelled", "command_cancelled")
            raise
        except BaseException:
            command.outcome("error", "command_failed")
            raise
        finally:
            operation.outcome(command.outcome_name, command.error_code)
            attributes = command.attributes | {
                "rpc.method": safe_method,
                "outcome": command.outcome_name,
            }
            if command.error_code is not None:
                attributes["error_code"] = command.error_code
            telemetry.count("trellis.runtime.commands", attributes=attributes)
            telemetry.observe(
                "trellis.runtime.command_duration", time.monotonic() - started, attributes
            )
            telemetry.event(
                "runtime.command.finished",
                attributes,
                error=command.outcome_name in {"error", "rejected"},
            )
