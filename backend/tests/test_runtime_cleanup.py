import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import anyio
import pytest
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from app.api.routes import runtime as runtime_routes
from app.domain.runtime import RunEvent, RunEventType, RunStatus
from app.infrastructure.runtime_events import RuntimeEventHub, RuntimeEventSubscription


class RecordingHub(RuntimeEventHub):
    def __init__(self, cleanup_child: Callable[[], Awaitable[None]] | None = None):
        super().__init__()
        self.relay_started = asyncio.Event()
        self._cleanup_child = cleanup_child

    def subscribe(self, run_id: str) -> RuntimeEventSubscription:
        hub = self

        class RecordingSubscription(RuntimeEventSubscription):
            async def receive(self) -> RunEvent | None:
                hub.relay_started.set()
                try:
                    return await super().receive()
                finally:
                    if hub._cleanup_child is not None:
                        await hub._cleanup_child()

        subscription = RecordingSubscription(run_id, 256)
        self._subscribers.setdefault(run_id, set()).add(subscription)
        return subscription


class PeerSocket:
    def __init__(
        self,
        hub: RecordingHub,
        receive_after_requests: Callable[[], Awaitable[str]],
        *,
        verify_token: Callable[[str], Awaitable[SimpleNamespace]] | None = None,
        send_payload: Callable[[object], Awaitable[None]] | None = None,
        close_socket: Callable[[int], Awaitable[None]] | None = None,
        resume_run: bool = True,
    ):
        run = SimpleNamespace(
            id="test-run",
            model_id="test-model",
            status=RunStatus.QUEUED,
            last_event_sequence=0,
            budget_preset="conservative",
            max_model_calls=1,
            max_tool_calls=1,
            max_total_tokens=10,
            max_cost_usd=0.1,
            deadline_at="2026-10-13T00:00:00Z",
        )

        class FakeRunService:
            async def get_run(self, run_id: str):
                assert run_id == "test-run"
                return run

            async def list_run_events(self, run_id: str, after_sequence: int):
                return []

        account = SimpleNamespace(run_service=FakeRunService(), event_hub=hub)
        verified = SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            expires_at=None,
        )

        class FakeVerifier:
            async def verify_token(self, token: str):
                assert token == "explicit-fake-token"
                if verify_token is not None:
                    return await verify_token(token)
                return verified

        class FakeRegistry:
            async def get(self, user_id: UUID, **kwargs: object):
                assert user_id == verified.id
                return account

        self.app = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(environment="test", web_origin=None),
                auth_verifier=FakeVerifier(),
                account_registry=FakeRegistry(),
            )
        )
        self.headers = {"origin": "http://localhost:3000"}
        self._receive_after_requests = receive_after_requests
        self._send_payload = send_payload
        self._close_socket = close_socket
        requests = [
            {
                "jsonrpc": "2.0",
                "id": "authenticate",
                "method": "auth.authenticate",
                "params": {"accessToken": "explicit-fake-token"},
            },
        ]
        if resume_run:
            requests.append(
                {
                    "jsonrpc": "2.0",
                    "id": "resume",
                    "method": "run.resume",
                    "params": {"runId": "test-run", "afterSequence": 0},
                }
            )
        self._requests = iter(requests)

    async def accept(self) -> None:
        pass

    async def close(self, code: int) -> None:
        if self._close_socket is not None:
            await self._close_socket(code)

    async def send_json(self, payload: object) -> None:
        if self._send_payload is not None:
            await self._send_payload(payload)

    async def receive_text(self) -> str:
        request = next(self._requests, None)
        if request is not None:
            return json.dumps(request)
        return await self._receive_after_requests()


def assert_runtime_children_gone(hub: RecordingHub) -> None:
    assert hub._subscribers == {}
    children = [
        task for task in asyncio.all_tasks() if task.get_name().startswith("trellis-websocket")
    ]
    assert children == []


def test_slow_send_closes_socket_and_joins_runtime_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_routes, "_SEND_TIMEOUT_SECONDS", 0.01, raising=False)

    async def exercise() -> None:
        send_started = asyncio.Event()
        send_cancelled = asyncio.Event()
        closed_codes: list[int] = []

        async def send_payload(payload: object) -> None:
            if isinstance(payload, dict) and payload.get("method") == "run.event":
                send_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    send_cancelled.set()

        async def close_socket(code: int) -> None:
            closed_codes.append(code)

        async def receive_after_resume() -> str:
            await hub.relay_started.wait()
            await hub.publish(RunEvent("test-run", 1, RunEventType.STARTED, 1, {}, "created"))
            await asyncio.Event().wait()
            raise AssertionError("slow transport must cancel the socket receiver")

        hub = RecordingHub()
        socket = PeerSocket(
            hub, receive_after_resume, send_payload=send_payload, close_socket=close_socket
        )
        handler = asyncio.create_task(runtime_routes.runtime_websocket(cast(WebSocket, socket)))
        try:
            await asyncio.wait_for(send_started.wait(), timeout=1)
            await asyncio.wait_for(asyncio.shield(handler), timeout=0.25)
        finally:
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
        assert send_cancelled.is_set()
        assert closed_codes == [1013]
        assert_runtime_children_gone(hub)

    asyncio.run(exercise())


def test_send_lock_wait_has_its_own_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_routes, "_SEND_TIMEOUT_SECONDS", 0.01, raising=False)

    async def exercise() -> None:
        send_started = asyncio.Event()
        closed_codes: list[int] = []

        async def send_payload(payload: object) -> None:
            if isinstance(payload, dict) and payload.get("method") == "run.event":
                send_started.set()
                await asyncio.Event().wait()

        async def close_socket(code: int) -> None:
            closed_codes.append(code)

        async def receive_after_resume() -> str:
            await hub.relay_started.wait()
            await hub.publish(RunEvent("test-run", 1, RunEventType.STARTED, 1, {}, "created"))
            await send_started.wait()
            return json.dumps({"jsonrpc": "2.0", "id": "locked-send", "method": "unknown"})

        hub = RecordingHub()
        socket = PeerSocket(
            hub, receive_after_resume, send_payload=send_payload, close_socket=close_socket
        )
        handler = asyncio.create_task(runtime_routes.runtime_websocket(cast(WebSocket, socket)))
        try:
            await asyncio.wait_for(send_started.wait(), timeout=1)
            await asyncio.wait_for(asyncio.shield(handler), timeout=0.25)
        finally:
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
        assert closed_codes == [1013]
        assert_runtime_children_gone(hub)

    asyncio.run(exercise())


def test_authentication_reply_has_a_send_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_routes, "_SEND_TIMEOUT_SECONDS", 0.01)

    async def exercise() -> None:
        closed_codes: list[int] = []

        async def send_payload(payload: object) -> None:
            if isinstance(payload, dict) and payload.get("id") == "authenticate":
                await asyncio.Event().wait()

        async def close_socket(code: int) -> None:
            closed_codes.append(code)

        async def receive_after_auth() -> str:
            raise AssertionError("authentication send must time out first")

        hub = RecordingHub()
        socket = PeerSocket(
            hub,
            receive_after_auth,
            send_payload=send_payload,
            close_socket=close_socket,
            resume_run=False,
        )
        handler = asyncio.create_task(runtime_routes.runtime_websocket(cast(WebSocket, socket)))
        try:
            await asyncio.wait_for(asyncio.shield(handler), timeout=0.25)
        finally:
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
        assert closed_codes == [1013]
        assert_runtime_children_gone(hub)

    asyncio.run(exercise())


@pytest.mark.parametrize("method", ["run.resume", "run.start"])
def test_socket_limits_active_run_subscriptions(method: str) -> None:
    async def exercise() -> None:
        responses: list[object] = []
        created_runs: list[str] = []
        requests = iter(range(1, 9))

        async def receive_more_requests() -> str:
            index = next(requests, None)
            if index is None:
                raise WebSocketDisconnect(code=1000)
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": f"resume-{index}",
                    "method": method,
                    "params": (
                        {"runId": f"run-{index}", "afterSequence": 0}
                        if method == "run.resume"
                        else {
                            "sessionId": "test-session",
                            "clientRequestId": f"run-{index}",
                            "content": "Answer",
                        }
                    ),
                }
            )

        async def send_payload(payload: object) -> None:
            responses.append(payload)

        hub = RecordingHub()
        socket = PeerSocket(hub, receive_more_requests, send_payload=send_payload)
        account = await socket.app.state.account_registry.get(
            UUID("00000000-0000-0000-0000-000000000001")
        )
        run = await account.run_service.get_run("test-run")

        async def get_run(run_id: str) -> SimpleNamespace:
            return SimpleNamespace(**(vars(run) | {"id": run_id}))

        async def create_run(
            session_id: str, client_request_id: str, content: str, **kwargs: object
        ) -> SimpleNamespace:
            created_runs.append(client_request_id)
            return await get_run(client_request_id)

        account.run_service.get_run = get_run
        account.run_service.create_run = create_run
        await runtime_routes.runtime_websocket(cast(WebSocket, socket))
        last_response = cast(dict[str, object], responses[-1])
        assert last_response["id"] == "resume-8"
        assert "error" in last_response
        error = cast(dict[str, object], last_response["error"])
        assert error["data"] == {"code": "subscription_limit", "status": 429}
        if method == "run.start":
            assert len(created_runs) == 7
        assert_runtime_children_gone(hub)

    asyncio.run(exercise())


async def exercise_cancelled_peer(hub: RecordingHub, *, peer_disconnect: bool) -> bool:
    with anyio.CancelScope() as scope:

        async def receive_after_resume() -> str:
            await asyncio.wait_for(hub.relay_started.wait(), timeout=1)
            # TestClient sends disconnect, then cancels its AnyIO scope. Schedule
            # that cancellation before the handler's plain child-task cancel.
            asyncio.get_running_loop().call_soon(scope.cancel)
            if peer_disconnect:
                raise WebSocketDisconnect(code=1000)
            await asyncio.Event().wait()
            raise AssertionError("socket receive must be cancelled")

        socket = PeerSocket(hub, receive_after_resume)
        await runtime_routes.runtime_websocket(cast(WebSocket, socket))
        if not peer_disconnect:
            raise AssertionError("handler must propagate outer cancellation")

    assert_runtime_children_gone(hub)
    return scope.cancelled_caught


def test_peer_disconnect_cancellation_joins_runtime_children() -> None:
    async def exercise() -> None:
        await exercise_cancelled_peer(RecordingHub(), peer_disconnect=True)

    asyncio.run(exercise())


def test_outer_receive_cancellation_propagates_after_joining_runtime_children() -> None:
    async def exercise() -> None:
        assert await exercise_cancelled_peer(RecordingHub(), peer_disconnect=False)

    asyncio.run(exercise())


def test_cleanup_deadline_cancels_slow_runtime_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_routes, "_CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def exercise() -> None:
        cleanup_started = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def slow_cleanup() -> None:
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_finished.set()

        hub = RecordingHub(slow_cleanup)
        # The child has no release event. Only the cleanup deadline can finish
        # this call before the independent test watchdog expires.
        async with asyncio.timeout(1):
            await exercise_cancelled_peer(hub, peer_disconnect=True)
        assert cleanup_started.is_set()
        assert cleanup_finished.is_set()

    asyncio.run(exercise())


def test_disconnect_during_auth_close_does_not_cancel_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_routes, "_AUTH_RECHECK_SECONDS", 0)

    async def exercise() -> None:
        close_started = asyncio.Event()
        verification_count = 0

        async def verify_token(token: str) -> SimpleNamespace:
            nonlocal verification_count
            verification_count += 1
            if verification_count > 1:
                raise HTTPException(status_code=401)
            return SimpleNamespace(
                id=UUID("00000000-0000-0000-0000-000000000001"),
                expires_at=None,
            )

        async def send_payload(payload: object) -> None:
            if isinstance(payload, dict) and "error" in payload:
                close_started.set()
                await asyncio.Event().wait()

        async def receive_disconnect() -> str:
            await asyncio.wait_for(close_started.wait(), timeout=1)
            raise WebSocketDisconnect(code=1000)

        hub = RecordingHub()
        socket = PeerSocket(
            hub,
            receive_disconnect,
            verify_token=verify_token,
            send_payload=send_payload,
            resume_run=False,
        )
        async with asyncio.timeout(1):
            await runtime_routes.runtime_websocket(cast(WebSocket, socket))
        assert verification_count == 2
        assert_runtime_children_gone(hub)

    asyncio.run(exercise())


def test_external_cancellation_during_auth_close_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_routes, "_AUTH_RECHECK_SECONDS", 60)

    async def exercise() -> None:
        close_started = asyncio.Event()
        verification_count = 0

        async def verify_token(token: str) -> SimpleNamespace:
            nonlocal verification_count
            verification_count += 1
            if verification_count > 1:
                raise HTTPException(status_code=401)
            return SimpleNamespace(
                id=UUID("00000000-0000-0000-0000-000000000001"),
                expires_at=None,
            )

        async def send_payload(payload: object) -> None:
            if isinstance(payload, dict) and "error" in payload:
                close_started.set()
                await asyncio.Event().wait()

        async def unexpected_receive() -> str:
            raise AssertionError("the pending auth close must be cancelled")

        hub = RecordingHub()
        socket = PeerSocket(
            hub,
            unexpected_receive,
            verify_token=verify_token,
            send_payload=send_payload,
        )
        handler = asyncio.create_task(runtime_routes.runtime_websocket(cast(WebSocket, socket)))
        async with asyncio.timeout(1):
            await close_started.wait()
            handler.cancel("external shutdown")
            with pytest.raises(asyncio.CancelledError, match="external shutdown"):
                await handler
        assert verification_count == 2
        assert_runtime_children_gone(hub)

    asyncio.run(exercise())


def test_external_cancellation_racing_auth_monitor_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_routes, "_AUTH_RECHECK_SECONDS", 0)

    async def exercise() -> None:
        receive_started = asyncio.Event()
        external_cancel_requested = asyncio.Event()
        verification_count = 0
        handler: asyncio.Task[None] | None = None

        async def verify_token(token: str) -> SimpleNamespace:
            nonlocal verification_count
            verification_count += 1
            if verification_count > 1:
                raise HTTPException(status_code=401)
            return SimpleNamespace(
                id=UUID("00000000-0000-0000-0000-000000000001"),
                expires_at=None,
            )

        def request_external_cancel() -> None:
            assert handler is not None
            external_cancel_requested.set()
            handler.cancel("external shutdown")

        async def close_socket(code: int) -> None:
            assert code == 4401
            # The monitor cancels synchronously after close returns. This callback
            # runs before the handler wakes, so the first cancellation's message
            # wins while the second cancellation must still propagate.
            asyncio.get_running_loop().call_soon(request_external_cancel)

        async def receive_message() -> str:
            receive_started.set()
            await asyncio.Event().wait()
            raise AssertionError("socket receive must be cancelled")

        hub = RecordingHub()
        socket = PeerSocket(
            hub,
            receive_message,
            verify_token=verify_token,
            close_socket=close_socket,
            resume_run=False,
        )
        handler = asyncio.create_task(runtime_routes.runtime_websocket(cast(WebSocket, socket)))
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await handler
        assert receive_started.is_set()
        assert external_cancel_requested.is_set()
        assert handler.cancelling() == 2
        assert verification_count == 2
        assert_runtime_children_gone(hub)

    asyncio.run(exercise())
