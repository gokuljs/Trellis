import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from app.application.errors import ApplicationError
from app.application.tools import ToolExecutionError, ToolRegistration, ToolRegistry
from app.core.config import Settings
from app.domain.runtime import ModelToolCall
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource
from app.infrastructure.telemetry import TelemetryRuntime

CANARY = "private-database-and-tool-content-839273"
USER_ID = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def recorded() -> Iterator[tuple[TelemetryRuntime, InMemorySpanExporter]]:
    spans = InMemorySpanExporter()
    tracer = TracerProvider(shutdown_on_exit=False)
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    runtime = TelemetryRuntime(
        tracer,
        LoggerProvider(shutdown_on_exit=False),
        MeterProvider(shutdown_on_exit=False),
    )
    yield runtime, spans
    asyncio.run(runtime.close())


def test_database_operations_preserve_requests_and_exclude_private_values(recorded) -> None:
    telemetry, spans = recorded
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"private": CANARY}])

    async def exercise() -> None:
        source = VerifiedTokenSource(USER_ID)
        source.record_verified(CANARY, USER_ID, datetime.now(UTC) + timedelta(hours=1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(
                Settings(
                    _env_file=None,
                    supabase_url="https://test.supabase.co",
                    supabase_publishable_key="sb_publishable_test",
                ),
                USER_ID,
                source,
                client,
                telemetry=telemetry,
            )
            with telemetry.operation("run.execute", {"runId": "owned-run"}):
                assert await repository._read(
                    "runs", select="*", filters={"private": CANARY, "limit": "1"}
                ) == [{"private": CANARY}]
                await repository._rpc("create_run", {"message": CANARY}, capability=CANARY)
                await repository._request("POST", f"rpc/{CANARY}", payload={"private": CANARY})

    asyncio.run(exercise())
    operations = [span for span in spans.get_finished_spans() if span.name == "db.request"]
    assert [span.attributes["db.operation"] for span in operations] == [
        "select",
        "create_run",
        "other",
    ]
    assert all(span.parent is not None for span in operations)
    assert requests[0].headers["Authorization"] == f"Bearer {CANARY}"
    assert CANARY in str(requests[0].url)
    assert CANARY.encode() in requests[1].content
    assert CANARY.encode() not in encode_spans(spans.get_finished_spans()).SerializeToString()


@pytest.mark.parametrize("mode", ["denied", "invalid_json", "cancelled"])
def test_database_failures_and_cancellation_are_classified_without_content(recorded, mode) -> None:
    telemetry, spans = recorded

    async def respond(_request: httpx.Request) -> httpx.Response:
        if mode == "cancelled":
            raise asyncio.CancelledError(CANARY)
        return httpx.Response(401 if mode == "denied" else 200, content=CANARY)

    async def exercise() -> None:
        source = VerifiedTokenSource(USER_ID)
        source.record_verified(CANARY, USER_ID, datetime.now(UTC) + timedelta(hours=1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(
                Settings(
                    _env_file=None,
                    supabase_url="https://test.supabase.co",
                    supabase_publishable_key="sb_publishable_test",
                ),
                USER_ID,
                source,
                client,
                telemetry=telemetry,
            )
            error_type = (
                asyncio.CancelledError
                if mode == "cancelled"
                else ApplicationError
                if mode == "denied"
                else RuntimeError
            )
            with pytest.raises(error_type):
                await repository._request("GET", "runs")

    asyncio.run(exercise())
    span = spans.get_finished_spans()[0]
    assert span.attributes["outcome"] == ("cancelled" if mode == "cancelled" else "error")
    assert not span.events
    assert CANARY.encode() not in encode_spans([span]).SerializeToString()


def test_tool_validation_never_exports_arguments_or_unknown_names(recorded) -> None:
    telemetry, spans = recorded

    class Arguments(BaseModel):
        count: int

    class Executor:
        async def execute(self, name, arguments, workspace_root):
            return CANARY, False

    tools = ToolRegistry(
        additional_tools=(ToolRegistration("lookup", "Look up a value", Arguments, Executor()),),
        telemetry=telemetry,
    )
    with pytest.raises(ToolExecutionError):
        tools.check_call(ModelToolCall("provider-id", CANARY, {"private": CANARY}), None)
    invalid = ModelToolCall("provider-id", "lookup", {"count": CANARY})
    result = asyncio.run(tools.execute(invalid, None))
    assert result.is_error
    valid = ModelToolCall("provider-id", "lookup", {"count": 1})
    result = asyncio.run(tools.execute(valid, None))
    assert result.content == CANARY
    operations = spans.get_finished_spans()
    assert any(span.attributes.get("outcome") == "rejected" for span in operations)
    assert any(span.attributes.get("outcome") == "completed" for span in operations)
    assert CANARY.encode() not in encode_spans(operations).SerializeToString()
