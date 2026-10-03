import asyncio
import json
import logging
from collections.abc import Mapping
from typing import cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.domain.runtime import RunEvent, RunEventType, is_terminal_run_status
from app.infrastructure.runtime_events import RuntimeEventHub, RuntimeEventSubscription

logger = logging.getLogger(__name__)
router = APIRouter(tags=["runtime"])
_TERMINAL_EVENTS = frozenset(
    {
        RunEventType.COMPLETED,
        RunEventType.FAILED,
        RunEventType.CANCELLED,
        RunEventType.INTERRUPTED,
    }
)


class RpcFault(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _error_response(
    request_id: str | int | None,
    code: int,
    message: str,
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _parse_request(
    raw: str,
) -> tuple[str, dict[str, object], str | int | None, bool]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise RpcFault(-32700, "Parse error") from None
    if not isinstance(payload, dict):
        raise RpcFault(-32600, "Invalid Request")

    has_id = "id" in payload
    request_id = payload.get("id")
    if has_id and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
        raise RpcFault(-32600, "Invalid Request")
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        raise RpcFault(-32600, "Invalid Request")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise RpcFault(-32600, "Invalid Request")
    return (
        cast(str, payload["method"]),
        cast(dict[str, object], params),
        cast(str | int | None, request_id),
        has_id,
    )


def _required_string(params: Mapping[str, object], name: str, *, maximum: int) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RpcFault(-32602, f"Invalid params: {name}")
    return value


def _event_notification(event: RunEvent) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "run.event",
        "params": {
            "runId": event.run_id,
            "sequence": event.sequence,
            "eventType": event.event_type.value,
            "eventVersion": event.event_version,
            "data": event.data,
            "createdAt": event.created_at,
        },
    }


@router.websocket("/api/runtime")
async def runtime_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    service: RunService = websocket.app.state.run_service
    event_hub: RuntimeEventHub = websocket.app.state.runtime_event_hub
    send_lock = asyncio.Lock()
    active_subscriptions: dict[str, tuple[RuntimeEventSubscription, asyncio.Task[None]]] = {}

    async def send(payload: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def relay_events(
        run_id: str,
        after_sequence: int,
        subscription: RuntimeEventSubscription,
    ) -> None:
        sequence = after_sequence
        try:
            run = await service.get_run(run_id)
            if run is None:
                return
            while True:
                batch = await service.list_run_events(run_id, sequence)
                for event in batch:
                    if event.sequence <= sequence:
                        continue
                    await send(_event_notification(event))
                    sequence = event.sequence
                    if event.event_type in _TERMINAL_EVENTS:
                        return
                if len(batch) < 500:
                    break

            if is_terminal_run_status(run.status):
                return
            while True:
                event = await subscription.receive()
                if event is None:
                    await send(
                        {
                            "jsonrpc": "2.0",
                            "method": "run.replay_required",
                            "params": {"runId": run_id, "afterSequence": sequence},
                        }
                    )
                    return
                if event.sequence <= sequence:
                    continue
                await send(_event_notification(event))
                sequence = event.sequence
                if event.event_type in _TERMINAL_EVENTS:
                    return
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            return
        except Exception as error:
            logger.error(
                "Could not relay runtime events for %s (error type: %s)",
                run_id,
                type(error).__name__,
            )
        finally:
            event_hub.unsubscribe(subscription)
            active = active_subscriptions.get(run_id)
            if active is not None and active[0] is subscription:
                del active_subscriptions[run_id]

    async def subscribe(run_id: str, after_sequence: int) -> None:
        existing = active_subscriptions.pop(run_id, None)
        if existing is not None:
            event_hub.unsubscribe(existing[0])
            existing[1].cancel()
            await asyncio.gather(existing[1], return_exceptions=True)
        subscription = event_hub.subscribe(run_id)
        task = asyncio.create_task(
            relay_events(run_id, after_sequence, subscription),
            name=f"trellis-websocket-{run_id}",
        )
        active_subscriptions[run_id] = (subscription, task)

    async def dispatch(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "run.start":
            session_id = _required_string(params, "sessionId", maximum=200)
            client_request_id = _required_string(params, "clientRequestId", maximum=200)
            content = _required_string(params, "content", maximum=100_000)
            turn_id = params.get("turnId")
            if turn_id is not None and not isinstance(turn_id, str):
                raise RpcFault(-32602, "Invalid params: turnId")
            run = await service.create_run(
                session_id,
                client_request_id,
                content,
                turn_id=turn_id,
            )
            await subscribe(run.id, 0)
            result: dict[str, object] = {
                "runId": run.id,
                "status": run.status.value,
                "lastSequence": run.last_event_sequence,
            }
        elif method == "run.resume":
            run_id = _required_string(params, "runId", maximum=200)
            after_sequence = params.get("afterSequence", 0)
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or after_sequence < 0
            ):
                raise RpcFault(-32602, "Invalid params: afterSequence")
            run = await service.get_run(run_id)
            if run is None:
                raise ApplicationError("run_not_found", "Run not found.")
            await subscribe(run.id, after_sequence)
            result = {
                "runId": run.id,
                "status": run.status.value,
                "lastSequence": run.last_event_sequence,
            }
        else:
            raise RpcFault(-32601, "Method not found")

        return result

    try:
        while True:
            raw = await websocket.receive_text()
            request_id: str | int | None = None
            has_id = True
            try:
                method, params, request_id, has_id = _parse_request(raw)
                result = await dispatch(method, params)
                if has_id:
                    await send({"jsonrpc": "2.0", "id": request_id, "result": result})
            except RpcFault as error:
                if has_id:
                    await send(_error_response(request_id, error.code, error.message, error.data))
            except ApplicationError as error:
                if has_id:
                    await send(
                        _error_response(
                            request_id,
                            -32000,
                            error.message,
                            {"code": error.code},
                        )
                    )
            except Exception as error:
                logger.error(
                    "Runtime JSON-RPC request failed (error type: %s)",
                    type(error).__name__,
                )
                if has_id:
                    await send(_error_response(request_id, -32603, "Internal error"))
    except WebSocketDisconnect:
        pass
    finally:
        subscriptions = tuple(active_subscriptions.values())
        active_subscriptions.clear()
        for subscription, task in subscriptions:
            event_hub.unsubscribe(subscription)
            task.cancel()
        if subscriptions:
            await asyncio.gather(
                *(task for _subscription, task in subscriptions),
                return_exceptions=True,
            )
