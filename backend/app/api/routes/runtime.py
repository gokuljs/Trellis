import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from typing import cast
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.application.budgets import BudgetPreset
from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.domain.runtime import (
    RunEvent,
    RunEventType,
    ToolApprovalDecision,
    is_terminal_run_status,
)
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
_WEB_ORIGINS = frozenset({"http://localhost:3000", "http://127.0.0.1:3000"})
_AUTH_TIMEOUT_SECONDS = 5
_AUTH_RECHECK_SECONDS = 30
_MAX_AUTH_FRAME_BYTES = 20_000


def _allowed_origins(environment: str, web_origin: str | None) -> frozenset[str]:
    origins = set(_WEB_ORIGINS) if environment in {"development", "test"} else set()
    if web_origin is None or web_origin != web_origin.strip() or "\\" in web_origin:
        return frozenset(origins)
    try:
        parts = urlsplit(web_origin)
        port = parts.port
    except ValueError:
        return frozenset(origins)
    if (
        parts.scheme == "https"
        and parts.hostname
        and parts.username is None
        and parts.password is None
        and port != 0
        and "*" not in web_origin
        and web_origin == f"https://{parts.netloc}"
    ):
        origins.add(web_origin)
    return frozenset(origins)


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


def _decode_requests(raw: str) -> tuple[list[object], bool]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise RpcFault(-32700, "Parse error") from None
    if isinstance(payload, list):
        if not payload or len(payload) > 32:
            raise RpcFault(-32600, "Invalid Request")
        return payload, True
    return [payload], False


def _parse_request(
    payload: object,
) -> tuple[str, dict[str, object], str | int | None, bool]:
    if not isinstance(payload, dict):
        raise RpcFault(-32600, "Invalid Request")

    has_id = "id" in payload
    request_id = payload.get("id")
    if has_id and (
        isinstance(request_id, bool) or not isinstance(request_id, (str, int, type(None)))
    ):
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


def _run_result(run) -> dict[str, object]:
    return {
        "runId": run.id,
        "modelId": run.model_id,
        "status": run.status.value,
        "lastSequence": run.last_event_sequence,
        "budgetPreset": run.budget_preset,
        "limits": {
            "maxModelCalls": run.max_model_calls,
            "maxToolCalls": run.max_tool_calls,
            "maxTotalTokens": run.max_total_tokens,
            "maxCostUsd": run.max_cost_usd,
            "deadlineAt": run.deadline_at,
        },
    }


@router.websocket("/api/runtime")
async def runtime_websocket(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    if websocket.headers.get("origin") not in _allowed_origins(
        settings.environment, settings.web_origin
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        raw_auth = await asyncio.wait_for(websocket.receive_text(), timeout=_AUTH_TIMEOUT_SECONDS)
        if len(raw_auth.encode("utf-8")) > _MAX_AUTH_FRAME_BYTES:
            raise RpcFault(-32600, "Authentication frame is too large")
        auth_requests, is_batch = _decode_requests(raw_auth)
        if is_batch:
            raise RpcFault(-32600, "Authenticate before sending run requests")
        method, params, request_id, has_id = _parse_request(auth_requests[0])
        if method != "auth.authenticate" or not has_id:
            raise RpcFault(-32001, "Authenticate before sending run requests")
        token = _required_string(params, "accessToken", maximum=16_384)
    except TimeoutError, WebSocketDisconnect:
        await websocket.close(code=4401)
        return
    except RpcFault as error:
        await websocket.send_json(
            _error_response(
                None,
                error.code,
                error.message,
                {"code": "auth_required"},
            )
        )
        await websocket.close(code=4401)
        return

    try:
        verified = await websocket.app.state.auth_verifier.verify_token(token)
    except HTTPException as error:
        await websocket.send_json(
            _error_response(
                request_id,
                -32001,
                "Authentication failed",
                {"code": "auth_unavailable" if error.status_code == 503 else "auth_failed"},
            )
        )
        await websocket.close(code=4401)
        return
    try:
        account = await websocket.app.state.account_registry.get(
            verified.id, access_token=token, expires_at=verified.expires_at
        )
    except ApplicationError as error:
        if error.code in {
            "account_in_use",
            "account_activity_busy",
            "account_import_in_progress",
        }:
            status, close_code, rpc_code = 409, 4409, -32009
        elif error.code in {"reauth_required", "cloud_access_denied"}:
            status, close_code, rpc_code = 401, 4401, -32001
        else:
            status, close_code, rpc_code = 503, 1013, -32003
        await websocket.send_json(
            _error_response(
                request_id,
                rpc_code,
                error.message if status != 503 else "Account data is temporarily unavailable",
                {"code": error.code, "status": status},
            )
        )
        await websocket.close(code=close_code)
        return
    await websocket.send_json(
        {"jsonrpc": "2.0", "id": request_id, "result": {"userId": str(verified.id)}}
    )
    service: RunService = account.run_service
    event_hub: RuntimeEventHub = account.event_hub
    send_lock = asyncio.Lock()
    auth_lost = asyncio.Event()
    active_subscriptions: dict[str, tuple[RuntimeEventSubscription, asyncio.Task[None]]] = {}

    async def send(payload: object) -> None:
        async with send_lock:
            if auth_lost.is_set():
                raise WebSocketDisconnect(code=4401)
            await websocket.send_json(payload)

    async def identity_is_current() -> bool:
        try:
            refreshed = await asyncio.wait_for(
                websocket.app.state.auth_verifier.verify_token(token),
                timeout=_AUTH_TIMEOUT_SECONDS,
            )
        except HTTPException, TimeoutError:
            return False
        except Exception as error:
            logger.error("Runtime authentication check failed (%s)", type(error).__name__)
            return False
        if refreshed.id != verified.id or auth_lost.is_set():
            return False
        try:
            await websocket.app.state.account_registry.get(
                verified.id, access_token=token, expires_at=refreshed.expires_at
            )
        except Exception as error:
            logger.error("Runtime cloud token refresh failed (%s)", type(error).__name__)
            return False
        return True

    async def close_for_auth() -> None:
        if auth_lost.is_set():
            return
        auth_lost.set()
        try:
            async with asyncio.timeout(_AUTH_TIMEOUT_SECONDS):
                with suppress(RuntimeError, WebSocketDisconnect):
                    async with send_lock:
                        # The peer may already have disconnected while a check was in flight.
                        await websocket.close(code=4401)
        except TimeoutError:
            logger.error("Runtime authentication close timed out")

    handler_task = asyncio.current_task()

    async def monitor_auth() -> None:
        while True:
            await asyncio.sleep(_AUTH_RECHECK_SECONDS)
            if not await identity_is_current():
                try:
                    await close_for_auth()
                finally:
                    if handler_task is not None:
                        handler_task.cancel()
                return

    auth_task = asyncio.create_task(monitor_auth(), name="trellis-websocket-auth")

    async def relay_events(
        run_id: str,
        after_sequence: int,
        subscription: RuntimeEventSubscription,
    ) -> None:
        sequence = after_sequence

        async def emit_event(event: RunEvent) -> bool:
            nonlocal sequence
            if event.sequence <= sequence:
                return False
            if event.sequence != sequence + 1:
                await send(
                    {
                        "jsonrpc": "2.0",
                        "method": "run.replay_required",
                        "params": {"runId": run_id, "afterSequence": sequence},
                    }
                )
                return True
            await send(_event_notification(event))
            sequence = event.sequence
            return event.event_type in _TERMINAL_EVENTS

        try:
            run = await service.get_run(run_id)
            if run is None:
                return
            while True:
                batch = await service.list_run_events(run_id, sequence)
                for event in batch:
                    if await emit_event(event):
                        return
                if len(batch) < 500:
                    break

            if is_terminal_run_status(run.status):
                return
            while True:
                try:
                    event = await asyncio.wait_for(subscription.receive(), timeout=0.5)
                except TimeoutError:
                    # Durable events are authoritative. Polling closes the rare gap where
                    # persistence succeeds but in-process fanout is interrupted.
                    batch = await service.list_run_events(run_id, sequence)
                    for persisted_event in batch:
                        if await emit_event(persisted_event):
                            return
                    current_run = await service.get_run(run_id)
                    if current_run is None or is_terminal_run_status(current_run.status):
                        return
                    continue
                if event is None:
                    await send(
                        {
                            "jsonrpc": "2.0",
                            "method": "run.replay_required",
                            "params": {"runId": run_id, "afterSequence": sequence},
                        }
                    )
                    return
                if await emit_event(event):
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
        result: dict[str, object]
        if method == "run.start":
            session_id = _required_string(params, "sessionId", maximum=200)
            client_request_id = _required_string(params, "clientRequestId", maximum=200)
            content = _required_string(params, "content", maximum=100_000)
            turn_id = params.get("turnId")
            if turn_id is not None and not isinstance(turn_id, str):
                raise RpcFault(-32602, "Invalid params: turnId")
            budget_preset = params.get("budgetPreset")
            if budget_preset is not None and budget_preset not in ("conservative", "longer"):
                raise RpcFault(-32602, "Invalid params: budgetPreset")
            model_id = params.get("modelId")
            if model_id is not None and (
                not isinstance(model_id, str) or not model_id or len(model_id) > 200
            ):
                raise RpcFault(-32602, "Invalid params: modelId")
            run = await service.create_run(
                session_id,
                client_request_id,
                content,
                turn_id=turn_id,
                budget_preset=cast(BudgetPreset | None, budget_preset),
                model_id=cast(str | None, model_id),
            )
            await subscribe(run.id, 0)
            result = _run_result(run)
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
            if after_sequence > run.last_event_sequence:
                raise RpcFault(-32602, "Invalid params: afterSequence exceeds the run cursor")
            await subscribe(run.id, after_sequence)
            result = _run_result(run)
        elif method == "run.cancel":
            run_id = _required_string(params, "runId", maximum=200)
            after_sequence = params.get("afterSequence", 0)
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or after_sequence < 0
            ):
                raise RpcFault(-32602, "Invalid params: afterSequence")
            existing_run = await service.get_run(run_id)
            if existing_run is None:
                raise ApplicationError("run_not_found", "Run not found.")
            if after_sequence > existing_run.last_event_sequence:
                raise RpcFault(-32602, "Invalid params: afterSequence exceeds the run cursor")
            run = await service.cancel_run(run_id)
            await subscribe(run.id, after_sequence)
            result = _run_result(run)
        elif method == "run.respond":
            run_id = _required_string(params, "runId", maximum=200)
            tool_call_id = _required_string(params, "toolCallId", maximum=200)
            decision_value = _required_string(params, "decision", maximum=8)
            if decision_value not in {decision.value for decision in ToolApprovalDecision}:
                raise RpcFault(-32602, "Invalid params: decision")
            after_sequence = params.get("afterSequence", 0)
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or after_sequence < 0
            ):
                raise RpcFault(-32602, "Invalid params: afterSequence")
            existing_run = await service.get_run(run_id)
            if existing_run is None:
                raise ApplicationError("run_not_found", "Run not found.")
            if after_sequence > existing_run.last_event_sequence:
                raise RpcFault(-32602, "Invalid params: afterSequence exceeds the run cursor")
            run, tool_call = await service.respond_to_tool_approval(
                run_id, tool_call_id, ToolApprovalDecision(decision_value)
            )
            if run.id not in active_subscriptions:
                await subscribe(run.id, after_sequence)
            result = _run_result(run) | {
                "toolCallId": tool_call.id,
                "decision": decision_value,
            }
        else:
            raise RpcFault(-32601, "Method not found")

        return result

    try:
        while True:
            raw = await websocket.receive_text()
            if not await identity_is_current():
                await close_for_auth()
                return
            try:
                requests, is_batch = _decode_requests(raw)
            except RpcFault as error:
                await send(_error_response(None, error.code, error.message, error.data))
                continue

            responses: list[dict[str, object]] = []
            for index, request in enumerate(requests):
                if index and not await identity_is_current():
                    await close_for_auth()
                    return
                request_id: str | int | None = None
                has_id = True
                try:
                    method, params, request_id, has_id = _parse_request(request)
                    result = await dispatch(method, params)
                    if has_id:
                        responses.append({"jsonrpc": "2.0", "id": request_id, "result": result})
                except RpcFault as error:
                    if has_id:
                        responses.append(
                            _error_response(request_id, error.code, error.message, error.data)
                        )
                except ApplicationError as error:
                    if has_id:
                        responses.append(
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
                        responses.append(_error_response(request_id, -32603, "Internal error"))

            if responses:
                await send(responses if is_batch else responses[0])
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        if not auth_lost.is_set():
            raise
    finally:
        auth_task.cancel()
        await asyncio.gather(auth_task, return_exceptions=True)
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
