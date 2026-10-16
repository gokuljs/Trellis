import asyncio
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.exceptions import StarletteDeprecationWarning

from app.api.auth import VerifiedUser
from app.api.telemetry import TransportTelemetryMiddleware
from app.application.errors import ApplicationError
from app.core.config import Settings
from app.domain.runtime import RunStatus
from app.infrastructure.runtime_events import RuntimeEventHub
from app.infrastructure.telemetry import TelemetryRuntime
from app.main import create_app

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

CANARY = "transport-private-canary-92837465"
USER_ID = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def recorded() -> Iterator[tuple]:
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    reader = InMemoryMetricReader()
    tracer = TracerProvider(shutdown_on_exit=False)
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    logger = LoggerProvider(shutdown_on_exit=False)
    logger.add_log_record_processor(SimpleLogRecordProcessor(logs))
    meter = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    runtime = TelemetryRuntime(tracer, logger, meter)
    yield runtime, spans, logs, reader
    asyncio.run(runtime.close())


def application(runtime):
    app = create_app(Settings(_env_file=None, environment="test"), telemetry=runtime)

    @app.post("/api/probe/{item_id}")
    async def probe(item_id: int, request: Request):
        return {"item": item_id, "payload": await request.json()}

    @app.get("/api/crash")
    async def crash():
        raise RuntimeError(CANARY)

    return app


def assert_no_canary(recorded) -> None:
    _, spans, logs, reader = recorded
    payloads = [
        encode_spans(spans.get_finished_spans()).SerializeToString(),
        encode_logs(logs.get_finished_logs()).SerializeToString(),
    ]
    metrics = reader.get_metrics_data()
    if metrics is not None:
        payloads.append(encode_metrics(metrics).SerializeToString())
        for resource in metrics.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        assert not {"runId", "modelCallId", "toolCallId"} & point.attributes.keys()
    assert all(CANARY.encode() not in payload for payload in payloads)


def test_http_templates_trace_context_and_body_are_preserved(recorded) -> None:
    runtime, exporter, _, _ = recorded
    app = application(runtime)
    with TestClient(app) as client:
        response = client.post(
            f"/api/probe/42?private={CANARY}",
            headers={
                "Authorization": f"Bearer {CANARY}",
                "x-private": CANARY,
                "traceparent": "00-00000000000000000000000000001234-0000000000005678-01",
                "baggage": f"private={CANARY}",
            },
            json={"content": CANARY},
        )
        assert response.status_code == 200
        assert response.json() == {"item": 42, "payload": {"content": CANARY}}
    span = exporter.get_finished_spans()[0]
    assert span.name == "http.request"
    assert span.kind is trace.SpanKind.SERVER
    assert span.context.trace_id == 0x1234
    assert span.parent.span_id == 0x5678
    assert span.parent.is_remote
    assert span.attributes["http.route"] == "/api/probe/{item_id}"
    assert span.attributes["http.request.method"] == "POST"
    assert span.attributes["http.response.status_code"] == 200
    assert span.attributes["outcome"] == "completed"
    assert_no_canary(recorded)


@pytest.mark.parametrize(
    "traceparent",
    [
        CANARY,
        "00-00000000000000000000000000000000-0000000000005678-01",
        "00-00000000000000000000000000001234-0000000000000000-01",
        "ff-00000000000000000000000000001234-0000000000005678-01",
        "00-00000000000000000000000000001234-0000000000005678-01-extra",
    ],
)
def test_invalid_trace_context_is_ignored(recorded, traceparent) -> None:
    runtime, exporter, _, _ = recorded
    with TestClient(application(runtime)) as client:
        assert client.post("/api/probe/1", json={}, headers={"traceparent": traceparent}).is_success
    assert exporter.get_finished_spans()[0].parent is None


def test_health_unmatched_and_validation_use_safe_routes(recorded) -> None:
    runtime, exporter, _, _ = recorded
    with TestClient(application(runtime)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 404
        assert client.get(f"/{CANARY}").status_code == 404
        assert client.post(f"/api/probe/{CANARY}", json={CANARY: CANARY}).status_code == 422
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert [span.attributes["http.route"] for span in spans] == [
        "unmatched",
        "/api/probe/{item_id}",
    ]
    assert [span.attributes["http.response.status_code"] for span in spans] == [404, 422]
    assert all(span.attributes["outcome"] == "client_error" for span in spans)
    assert all(span.status.status_code is trace.StatusCode.UNSET for span in spans)
    assert_no_canary(recorded)


def test_http_uncaught_exception_stays_an_error_without_exception_content(recorded) -> None:
    runtime, exporter, _, _ = recorded
    with TestClient(application(runtime), raise_server_exceptions=False) as client:
        assert client.get("/api/crash").status_code == 500
    span = exporter.get_finished_spans()[0]
    assert span.attributes["outcome"] == "error"
    assert span.attributes["http.response.status_code"] == 500
    assert span.status.status_code is trace.StatusCode.ERROR
    assert not span.events
    assert_no_canary(recorded)


@contextmanager
def authenticate(client, *, token=CANARY):
    with client.websocket_connect(
        "/api/runtime", headers={"origin": "http://localhost:3000"}
    ) as websocket:
        websocket.send_json(
            {
                "jsonrpc": "2.0",
                "id": CANARY,
                "method": "auth.authenticate",
                "params": {"accessToken": token},
            }
        )
        yield websocket


def fake_account(app, *, failure=None):
    verifier = AsyncMock()
    verifier.verify_token.return_value = VerifiedUser(USER_ID, None)
    if failure is not None:
        verifier.verify_token.side_effect = failure
    app.state.auth_verifier = verifier
    run = SimpleNamespace(
        id="owned-run-1",
        model_id="configured-model",
        status=RunStatus.COMPLETED,
        last_event_sequence=0,
        budget_preset="conservative",
        max_model_calls=4,
        max_tool_calls=4,
        max_total_tokens=1000,
        max_cost_usd=1.0,
        deadline_at="2030-01-01T00:00:00Z",
    )
    service = AsyncMock()
    service.create_run.return_value = run
    service.get_run.return_value = run
    service.respond_to_tool_approval.return_value = (run, SimpleNamespace(id="owned-tool-1"))
    service.list_run_events.return_value = []
    registry = AsyncMock()
    registry.get.return_value = SimpleNamespace(run_service=service, event_hub=RuntimeEventHub())
    app.state.account_registry = registry
    return service


def test_runtime_commands_are_short_safe_and_correlate_only_owned_results(recorded) -> None:
    runtime, exporter, logs, _ = recorded
    app = application(runtime)
    with TestClient(app) as client:
        fake_account(app)
        with authenticate(client) as websocket:
            assert websocket.receive_json()["result"]["userId"] == str(USER_ID)
            websocket.send_json(
                {"jsonrpc": "2.0", "id": CANARY, "method": CANARY, "params": {"runId": CANARY}}
            )
            assert websocket.receive_json()["error"]["code"] == -32601
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "run.start",
                    "params": {
                        "sessionId": "session",
                        "clientRequestId": CANARY,
                        "content": CANARY,
                    },
                }
            )
            assert websocket.receive_json()["result"]["runId"] == "owned-run-1"
            # A command span finishes while its WebSocket remains connected.
            assert len(exporter.get_finished_spans()) == 3
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "run.respond",
                    "params": {
                        "runId": "owned-run-1",
                        "toolCallId": CANARY,
                        "decision": "approved",
                    },
                }
            )
            assert websocket.receive_json()["result"]["toolCallId"] == "owned-tool-1"
    spans = exporter.get_finished_spans()
    assert [span.attributes["rpc.method"] for span in spans] == [
        "auth.authenticate",
        "unknown",
        "run.start",
        "run.respond",
    ]
    assert all(span.kind is trace.SpanKind.SERVER and span.parent is None for span in spans)
    assert spans[1].attributes["outcome"] == "rejected"
    assert spans[1].attributes["error_code"] == "rpc_method_not_found"
    assert "runId" not in spans[1].attributes
    assert spans[2].attributes["runId"] == "owned-run-1"
    assert spans[3].attributes["toolCallId"] == "owned-tool-1"
    records = [item.log_record for item in logs.get_finished_logs()]
    assert records[1].attributes["error_code"] == "rpc_method_not_found"
    assert records[2].attributes["runId"] == "owned-run-1"
    assert records[3].attributes["toolCallId"] == "owned-tool-1"
    assert_no_canary(recorded)


@pytest.mark.parametrize("failure", [HTTPException(401, CANARY), RuntimeError(CANARY)])
def test_runtime_auth_failures_are_safe_and_explicit(recorded, failure) -> None:
    runtime, exporter, _, _ = recorded
    app = application(runtime)
    with TestClient(app) as client:
        fake_account(app, failure=failure)
        with authenticate(client) as websocket:
            assert "error" in websocket.receive_json()
    span = exporter.get_finished_spans()[0]
    assert span.attributes["rpc.method"] == "auth.authenticate"
    assert span.attributes["outcome"] in {"rejected", "error"}
    assert span.status.status_code is trace.StatusCode.ERROR
    assert_no_canary(recorded)


def test_runtime_caught_application_errors_have_safe_outcomes(recorded) -> None:
    runtime, exporter, _, _ = recorded
    app = application(runtime)
    with TestClient(app) as client:
        service = fake_account(app)
        service.create_run.side_effect = ApplicationError(CANARY, CANARY)
        with authenticate(client) as websocket:
            websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "run.start",
                    "params": {
                        "sessionId": "session",
                        "clientRequestId": "request",
                        "content": CANARY,
                    },
                }
            )
            assert "error" in websocket.receive_json()
    span = exporter.get_finished_spans()[-1]
    assert span.attributes["outcome"] == "rejected"
    assert span.attributes["error_code"] == "application_rejected"
    assert_no_canary(recorded)


def test_runtime_parse_validation_and_internal_failures_are_observed(recorded) -> None:
    runtime, exporter, _, _ = recorded
    app = application(runtime)
    with TestClient(app) as client:
        service = fake_account(app)
        service.create_run.side_effect = RuntimeError(CANARY)
        with authenticate(client) as websocket:
            websocket.receive_json()
            websocket.send_text(CANARY)
            assert websocket.receive_json()["error"]["code"] == -32700
            websocket.send_json(
                {"jsonrpc": "2.0", "id": 2, "method": "run.start", "params": {"sessionId": CANARY}}
            )
            assert websocket.receive_json()["error"]["code"] == -32602
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "run.start",
                    "params": {
                        "sessionId": "session",
                        "clientRequestId": "request",
                        "content": CANARY,
                    },
                }
            )
            assert websocket.receive_json()["error"]["code"] == -32603
    spans = exporter.get_finished_spans()
    assert [span.attributes.get("error_code") for span in spans[1:]] == [
        "rpc_parse_error",
        "rpc_invalid_params",
        "command_failed",
    ]
    assert spans[-1].attributes["outcome"] == "error"
    assert_no_canary(recorded)


def test_runtime_malformed_authentication_is_observed(recorded) -> None:
    runtime, exporter, _, _ = recorded
    app = application(runtime)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as websocket,
    ):
        websocket.send_text(CANARY)
        assert websocket.receive_json()["error"]["data"]["code"] == "auth_required"
    span = exporter.get_finished_spans()[0]
    assert span.attributes["outcome"] == "rejected"
    assert span.attributes["error_code"] == "auth_required"
    assert_no_canary(recorded)


def test_middleware_preserves_asgi_cancellation(recorded) -> None:
    runtime, exporter, _, _ = recorded

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError(CANARY)

    middleware = TransportTelemetryMiddleware(cancelled)
    scope = {
        "type": "http",
        "path": "/cancel",
        "method": "GET",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(telemetry=runtime)),
    }
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(middleware(scope, AsyncMock(), AsyncMock()))
    assert exporter.get_finished_spans()[0].attributes["outcome"] == "cancelled"
    assert_no_canary(recorded)
