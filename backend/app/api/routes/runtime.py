import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from contextvars import Context
from typing import cast

import anyio
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.api.access import require_private_access
from app.api.telemetry import CommandObservation, runtime_command, transport_telemetry
from app.application.budgets import BudgetPreset
from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.application.telemetry import run_identifiers
from app.core.origins import allowed_web_origins
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
_AUTH_TIMEOUT_SECONDS = 5
_AUTH_RECHECK_SECONDS = 30
_AUTH_MONITOR_CANCEL_REASON = "runtime authentication ended"
_CLEANUP_TIMEOUT_SECONDS = 5
_SEND_TIMEOUT_SECONDS = 5
_MAX_ACTIVE_SUBSCRIPTIONS = 8
_SLOW_TRANSPORT_CANCEL_REASON = "runtime transport send timed out"
_MAX_AUTH_FRAME_BYTES = 20_000


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


def _authentication_fault(status: int = 503) -> RpcFault:
    if status in {400, 401, 403}:
        return RpcFault(
            -32001,
            "Your session expired. Please sign in again",
            data={"code": "auth_failed", "status": 401},
        )
    return RpcFault(
        -32003,
        "Authentication is temporarily unavailable. Please try again.",
        data={"code": "auth_unavailable", "status": 503},
    )


def _account_fault(error: ApplicationError) -> RpcFault:
    if error.code == "private_access_denied":
        return RpcFault(
            -32004,
            "This account does not have access to Trellis.",
            data={"code": "private_access_denied", "status": 403},
        )
    if error.code == "account_in_use":
        return RpcFault(
            -32009,
            "This account is open in another backend process.",
            data={"code": error.code, "status": 409},
        )
    if error.code in {"reauth_required", "cloud_access_denied"}:
        return RpcFault(
            -32001,
            "Your session expired. Please sign in again",
            data={"code": error.code, "status": 401},
        )
    return RpcFault(
        -32003,
        "Account data is temporarily unavailable. Please try again.",
        data={"code": error.code, "status": 503},
    )


def _access_close_code(error: RpcFault) -> int:
    status = (error.data or {}).get("status")
    return 4401 if status == 401 else 4403 if status == 403 else 4409 if status == 409 else 1013


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


async def _send_with_deadline(
    websocket: WebSocket,
    payload: object,
    send_lock: asyncio.Lock | None = None,
    *,
    before_send: Callable[[], None] | None = None,
) -> None:
    async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
        if send_lock is None:
            if before_send is not None:
                before_send()
            await websocket.send_json(payload)
        else:
            async with send_lock:
                if before_send is not None:
                    before_send()
                await websocket.send_json(payload)


async def _close_transport(websocket: WebSocket, code: int) -> None:
    with suppress(RuntimeError, WebSocketDisconnect, TimeoutError):
        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
            await websocket.close(code=code)


@router.websocket("/api/runtime")
async def runtime_websocket(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    telemetry = transport_telemetry(websocket.scope)
    if websocket.headers.get("origin") not in allowed_web_origins(
        settings.environment, settings.web_origin
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    async def send_initial(payload: object) -> bool:
        try:
            await _send_with_deadline(websocket, payload)
        except TimeoutError:
            await _close_transport(websocket, 1013)
            return False
        return True

    async def reject_access(error: RpcFault, rpc_id: str | int | None) -> None:
        if await send_initial(_error_response(rpc_id, error.code, error.message, error.data)):
            await _close_transport(websocket, _access_close_code(error))

    with runtime_command(telemetry, "auth.authenticate") as authentication:
        try:
            raw_auth = await asyncio.wait_for(
                websocket.receive_text(), timeout=_AUTH_TIMEOUT_SECONDS
            )
            if len(raw_auth.encode("utf-8")) > _MAX_AUTH_FRAME_BYTES:
                raise RpcFault(-32600, "Authentication frame is too large")
            auth_requests, is_batch = _decode_requests(raw_auth)
            if is_batch:
                raise RpcFault(-32600, "Authenticate before sending run requests")
            method, params, request_id, has_id = _parse_request(auth_requests[0])
            if method != "auth.authenticate" or not has_id:
                raise RpcFault(-32001, "Authenticate before sending run requests")
            token = _required_string(params, "accessToken", maximum=16_384)
        except TimeoutError:
            authentication.outcome("error", "auth_timeout")
            await reject_access(_authentication_fault(), None)
            return
        except WebSocketDisconnect:
            authentication.outcome("cancelled", "transport_disconnected")
            return
        except RpcFault as error:
            authentication.outcome("rejected", "auth_required")
            if await send_initial(
                _error_response(
                    None,
                    error.code,
                    error.message,
                    {"code": "auth_required"},
                )
            ):
                await _close_transport(websocket, 4401)
            return

        try:
            verified = await asyncio.wait_for(
                websocket.app.state.auth_verifier.verify_token(token), timeout=_AUTH_TIMEOUT_SECONDS
            )
        except HTTPException as error:
            authentication.outcome(
                "rejected" if error.status_code in {400, 401, 403} else "error",
                "auth_failed" if error.status_code in {400, 401, 403} else "auth_unavailable",
            )
            await reject_access(_authentication_fault(error.status_code), request_id)
            return
        except Exception as error:
            authentication.outcome("error", "auth_unavailable")
            logger.error("Runtime authentication failed (%s)", type(error).__name__)
            await reject_access(_authentication_fault(), request_id)
            return
        try:
            require_private_access(settings, verified.id)
            account = await asyncio.wait_for(
                websocket.app.state.account_registry.get(
                    verified.id, access_token=token, expires_at=verified.expires_at
                ),
                timeout=_AUTH_TIMEOUT_SECONDS,
            )
        except ApplicationError as error:
            authentication.outcome(
                "rejected",
                "private_access_denied"
                if error.code == "private_access_denied"
                else "account_unavailable",
            )
            await reject_access(_account_fault(error), request_id)
            return
        except Exception as error:
            authentication.outcome("error", "account_unavailable")
            logger.error("Runtime account startup failed (%s)", type(error).__name__)
            await reject_access(
                _account_fault(ApplicationError("database_unavailable", "")), request_id
            )
            return
        if not await send_initial(
            {"jsonrpc": "2.0", "id": request_id, "result": {"userId": str(verified.id)}}
        ):
            authentication.outcome("error", "transport_unavailable")
            return
    service: RunService = account.run_service
    event_hub: RuntimeEventHub = account.event_hub
    send_lock = asyncio.Lock()
    auth_lost = asyncio.Event()
    active_subscriptions: dict[str, tuple[RuntimeEventSubscription, asyncio.Task[None]]] = {}
    transport_lost = asyncio.Event()
    transport_cancel_requested = False
    handler_task = asyncio.current_task()

    async def close_slow_transport() -> None:
        nonlocal transport_cancel_requested
        if transport_lost.is_set():
            return
        transport_lost.set()
        await _close_transport(websocket, 1013)
        if (
            not teardown_started
            and handler_task is not None
            and handler_task is not asyncio.current_task()
            and not handler_task.cancelling()
        ):
            transport_cancel_requested = True
            handler_task.cancel(_SLOW_TRANSPORT_CANCEL_REASON)

    async def send(payload: object) -> None:
        def check_connected() -> None:
            if auth_lost.is_set() or transport_lost.is_set():
                raise WebSocketDisconnect(code=4401 if auth_lost.is_set() else 1013)

        try:
            await _send_with_deadline(websocket, payload, send_lock, before_send=check_connected)
        except TimeoutError:
            await close_slow_transport()
            raise WebSocketDisconnect(code=1013) from None

    async def access_failure() -> RpcFault | None:
        try:
            refreshed = await asyncio.wait_for(
                websocket.app.state.auth_verifier.verify_token(token),
                timeout=_AUTH_TIMEOUT_SECONDS,
            )
        except HTTPException as error:
            return _authentication_fault(error.status_code)
        except Exception as error:
            logger.error("Runtime authentication check failed (%s)", type(error).__name__)
            return _authentication_fault()
        if refreshed.id != verified.id or auth_lost.is_set():
            return _authentication_fault(401)
        try:
            require_private_access(settings, refreshed.id)
            await asyncio.wait_for(
                websocket.app.state.account_registry.get(
                    verified.id, access_token=token, expires_at=refreshed.expires_at
                ),
                timeout=_AUTH_TIMEOUT_SECONDS,
            )
        except ApplicationError as error:
            return _account_fault(error)
        except Exception as error:
            logger.error("Runtime cloud token refresh failed (%s)", type(error).__name__)
            return _account_fault(ApplicationError("database_unavailable", ""))
        return None

    async def close_for_access(error: RpcFault) -> None:
        if auth_lost.is_set():
            return
        auth_lost.set()
        try:
            async with asyncio.timeout(_AUTH_TIMEOUT_SECONDS):
                with suppress(RuntimeError, WebSocketDisconnect):
                    async with send_lock:
                        # The peer may already have disconnected while a check was in flight.
                        await reject_access(error, None)
        except TimeoutError:
            logger.error("Runtime authentication close timed out")

    teardown_started = False

    async def monitor_auth() -> None:
        while True:
            await asyncio.sleep(_AUTH_RECHECK_SECONDS)
            if (error := await access_failure()) is not None:
                try:
                    await close_for_access(error)
                finally:
                    if (
                        not teardown_started
                        and handler_task is not None
                        and not handler_task.cancelling()
                    ):
                        handler_task.cancel(_AUTH_MONITOR_CANCEL_REASON)
                return

    auth_task = asyncio.create_task(
        monitor_auth(), name="trellis-websocket-auth", context=Context()
    )

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

        async def replay_persisted(through_sequence: int | None = None) -> bool:
            while True:
                previous_sequence = sequence
                batch = await service.list_run_events(run_id, sequence)
                for event in batch:
                    if await emit_event(event):
                        return True
                if len(batch) < 500 and (through_sequence is None or sequence >= through_sequence):
                    return False
                if sequence == previous_sequence:
                    await send(
                        {
                            "jsonrpc": "2.0",
                            "method": "run.replay_required",
                            "params": {"runId": run_id, "afterSequence": sequence},
                        }
                    )
                    return True

        try:
            with ExitStack() as scopes:
                run = await service.get_run(run_id)
                if run is None:
                    return
                scopes.enter_context(telemetry.correlate(run_identifiers(run)))
                if await replay_persisted(run.last_event_sequence):
                    return

                if is_terminal_run_status(run.status):
                    return
                while True:
                    try:
                        event = await asyncio.wait_for(subscription.receive(), timeout=0.5)
                    except TimeoutError:
                        # Durable events are authoritative. Polling closes the rare gap where
                        # persistence succeeds but in-process fanout is interrupted.
                        if await replay_persisted():
                            return
                        current_run = await service.get_run(run_id)
                        if current_run is None:
                            return
                        if is_terminal_run_status(current_run.status):
                            # Completion may commit after the last page was read. Drain
                            # through its durable cursor before ending the subscription.
                            if sequence < current_run.last_event_sequence:
                                await replay_persisted(current_run.last_event_sequence)
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

    def check_subscription_capacity(run_id: str | None = None) -> None:
        if (
            run_id not in active_subscriptions
            and len(active_subscriptions) >= _MAX_ACTIVE_SUBSCRIPTIONS
        ):
            raise RpcFault(
                -32010,
                "This connection has too many active run subscriptions.",
                data={"code": "subscription_limit", "status": 429},
            )

    async def subscribe(run_id: str, after_sequence: int) -> None:
        check_subscription_capacity(run_id)
        existing = active_subscriptions.pop(run_id, None)
        if existing is not None:
            event_hub.unsubscribe(existing[0])
            existing[1].cancel()
            await asyncio.gather(existing[1], return_exceptions=True)
        subscription = event_hub.subscribe(run_id)
        task = asyncio.create_task(
            relay_events(run_id, after_sequence, subscription),
            name=f"trellis-websocket-{run_id}",
            context=Context(),
        )
        active_subscriptions[run_id] = (subscription, task)

    async def dispatch(
        method: str, params: dict[str, object], command: CommandObservation
    ) -> dict[str, object] | None:
        result: dict[str, object]
        if method == "run.lookup":
            session_id = _required_string(params, "sessionId", maximum=200)
            client_request_id = _required_string(params, "clientRequestId", maximum=200)
            run = await service.lookup_run(session_id, client_request_id)
            if run is not None:
                command.correlate(run)
            return _run_result(run) if run is not None else None
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
            check_subscription_capacity()
            run = await service.create_run(
                session_id,
                client_request_id,
                content,
                turn_id=turn_id,
                budget_preset=cast(BudgetPreset | None, budget_preset),
                model_id=cast(str | None, model_id),
            )
            command.correlate(run)
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
            command.correlate(run)
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
            command.correlate(existing_run)
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
            command.correlate(existing_run)
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
            if (error := await access_failure()) is not None:
                await close_for_access(error)
                return
            try:
                requests, is_batch = _decode_requests(raw)
            except RpcFault as error:
                with runtime_command(telemetry, "unknown") as command:
                    command.outcome(
                        "rejected",
                        "rpc_parse_error" if error.code == -32700 else "rpc_invalid_request",
                    )
                await send(_error_response(None, error.code, error.message, error.data))
                continue

            responses: list[dict[str, object]] = []
            for index, request in enumerate(requests):
                if index and (error := await access_failure()) is not None:
                    await close_for_access(error)
                    return
                request_id: str | int | None = None
                has_id = True
                observed_method = request.get("method") if isinstance(request, dict) else None
                with runtime_command(
                    telemetry, observed_method if isinstance(observed_method, str) else "unknown"
                ) as command:
                    try:
                        method, params, request_id, has_id = _parse_request(request)
                        result = await dispatch(method, params, command)
                        if result is not None:
                            command.set_attributes({"runId": cast(str, result["runId"])})
                            if method == "run.respond":
                                command.set_attributes(
                                    {"toolCallId": cast(str, result["toolCallId"])}
                                )
                        if has_id:
                            responses.append({"jsonrpc": "2.0", "id": request_id, "result": result})
                    except RpcFault as error:
                        command.outcome(
                            "rejected",
                            {
                                -32700: "rpc_parse_error",
                                -32600: "rpc_invalid_request",
                                -32601: "rpc_method_not_found",
                                -32602: "rpc_invalid_params",
                            }.get(error.code, "rpc_rejected"),
                        )
                        if has_id:
                            responses.append(
                                _error_response(request_id, error.code, error.message, error.data)
                            )
                    except ApplicationError as error:
                        command.outcome("rejected", "application_rejected")
                        if error.code in {
                            "reauth_required",
                            "cloud_access_denied",
                            "database_unavailable",
                            "account_in_use",
                        }:
                            await close_for_access(_account_fault(error))
                            return
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
                        command.outcome("error", "command_failed")
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
    except asyncio.CancelledError as error:
        owned_transport_cancel = transport_cancel_requested and error.args in (
            (),
            (_SLOW_TRANSPORT_CANCEL_REASON,),
        )
        if (
            (error.args != (_AUTH_MONITOR_CANCEL_REASON,) and not owned_transport_cancel)
            or handler_task is None
            or handler_task.cancelling() != 1
        ):
            raise
        handler_task.uncancel()
    finally:
        teardown_started = True
        auth_task.cancel()
        subscriptions = tuple(active_subscriptions.values())
        active_subscriptions.clear()
        for subscription, task in subscriptions:
            event_hub.unsubscribe(subscription)
            task.cancel()
        # ASGI disconnect cancellation must not interrupt joining our child tasks.
        with anyio.move_on_after(_CLEANUP_TIMEOUT_SECONDS, shield=True):
            await asyncio.gather(
                auth_task,
                *(task for _subscription, task in subscriptions),
                return_exceptions=True,
            )
