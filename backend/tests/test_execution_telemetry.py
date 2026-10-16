import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application.errors import ApplicationError, ProviderError
from app.application.model_catalog import profile_for_model
from app.application.ports import RunRepository, StreamingProviderAdapter
from app.application.run_context import RunContext
from app.application.runs import RunService
from app.application.secret_redaction import SecretRedactor
from app.domain.context import ContextItem, ContextPreflight, NativeCompactionResult
from app.domain.models import ModelDescriptor, Session, UserProfile
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
    ToolCallRecord,
    ToolCallStatus,
    ToolResult,
)
from app.infrastructure.telemetry import TelemetryRuntime

CANARY = "private-runtime-body-729483"
MODEL = ModelDescriptor(
    "openai:gpt-5.5", "openai", "OpenAI", "responses", "gpt-5.5", "GPT", False, True, True, True
)


@pytest.fixture
def recorded() -> Iterator[
    tuple[TelemetryRuntime, InMemorySpanExporter, InMemoryLogRecordExporter]
]:
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    tracer = TracerProvider(shutdown_on_exit=False)
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    logger = LoggerProvider(shutdown_on_exit=False)
    logger.add_log_record_processor(SimpleLogRecordProcessor(logs))
    runtime = TelemetryRuntime(tracer, logger, MeterProvider(shutdown_on_exit=False))
    yield runtime, spans, logs
    asyncio.run(runtime.close())


def run_record(status: RunStatus = RunStatus.QUEUED) -> RunSnapshot:
    now = datetime.now(UTC)
    return RunSnapshot(
        "owned-run",
        "session",
        "turn",
        status,
        "openai",
        MODEL.id,
        MODEL.adapter_kind,
        MODEL.upstream_model_id,
        "input",
        None,
        "request",
        10,
        10,
        "longer",
        100000,
        10,
        (now + timedelta(minutes=5)).isoformat(),
        None,
        None,
        0,
        None,
        0,
        None,
        None,
        now.isoformat(),
        None,
        None,
    )


def model_record() -> ModelCallRecord:
    return ModelCallRecord(
        "owned-model-call",
        "owned-run",
        1,
        "openai",
        MODEL.id,
        MODEL.adapter_kind,
        ModelCallStatus.PENDING,
        {},
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        datetime.now(UTC).isoformat(),
        None,
    )


def request() -> ModelRequest:
    return ModelRequest(
        "openai",
        MODEL.id,
        MODEL.adapter_kind,
        MODEL.upstream_model_id,
        (ModelMessage("user", CANARY),),
        100,
    )


def plan() -> ContextPreflight:
    return ContextPreflight(
        request(), (), (), (), 100, "provider", "hash", 100, 1, 10000, True, False
    )


class Repository:
    def __init__(self) -> None:
        self.run = run_record()
        self.calls: list[ModelCallRecord] = []
        self.events: list[RunEvent] = []

    async def create_run(self, *args, **kwargs) -> RunSnapshot:
        return self.run

    async def get_run(self, _run_id: str) -> RunSnapshot:
        return self.run

    async def list_model_calls(self, _run_id: str) -> list[ModelCallRecord]:
        return self.calls.copy()

    async def list_tool_calls(self, _run_id: str) -> list[ToolCallRecord]:
        return []

    async def append_run_event(self, run_id, event_type, data) -> RunEvent:
        event = RunEvent(run_id, len(self.events) + 1, event_type, 1, data, "now")
        self.events.append(event)
        return event

    async def transition_run_record(self, run_id, status, event_type, data, **kwargs) -> RunEvent:
        self.run = replace(self.run, status=status)
        return await self.append_run_event(run_id, event_type, data)

    async def resume_approved_run(self, run_id) -> tuple[RunSnapshot, RunEvent]:
        self.run = replace(self.run, status=RunStatus.RUNNING)
        return self.run, await self.append_run_event(run_id, RunEventType.RESUMED, {})

    async def create_model_call(self, *args, **kwargs) -> ModelCallRecord:
        call = model_record()
        self.calls.append(call)
        return call

    async def update_model_call(self, call_id, status, **kwargs) -> ModelCallRecord:
        call = next(call for call in self.calls if call.id == call_id)
        fields = {key: value for key, value in kwargs.items() if key in call.__dataclass_fields__}
        call = replace(call, status=status, **fields)
        self.calls = [call]
        return call

    async def append_assistant_batch(self, run_id, call_id, index, text):
        return None, await self.append_run_event(
            run_id, RunEventType.ASSISTANT_DELTA, {"text": text}
        )

    async def complete_run(self, run_id, content):
        event = await self.transition_run_record(
            run_id, RunStatus.COMPLETED, RunEventType.COMPLETED, {}
        )
        return None, (event,)


def service(telemetry, repository, provider) -> RunService:
    sessions = Mock()
    sessions.get_session = AsyncMock(
        return_value=Session("session", "user", "title", "now", "now", 0)
    )
    settings = Mock()
    settings.list_models = AsyncMock(return_value=[MODEL])
    settings.get_selected_model_id = AsyncMock(return_value=MODEL.id)
    settings.get_default_budget_preset = AsyncMock(return_value="longer")
    profiles = Mock()
    profiles.get_profile = AsyncMock(return_value=UserProfile("user", None, None, "now", "now"))
    secrets = Mock()
    secrets.get = AsyncMock(return_value=CANARY)
    return RunService(
        repository,
        sessions,
        settings,
        profiles,
        secrets,
        {MODEL.adapter_kind: provider},
        telemetry=telemetry,
    )


class Provider:
    def __init__(self, failure: bool = False) -> None:
        self.failure = failure
        self.closed = False

    async def stream(self, *args):
        try:
            yield ModelStreamEvent("text_delta", text="answer")
            yield ModelStreamEvent(
                "usage",
                input_tokens=100,
                output_tokens=10,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
            if self.failure:
                raise ProviderError("provider_unavailable", CANARY)
            yield ModelStreamEvent("completed", finish_kind="final")
        finally:
            self.closed = True


@pytest.mark.parametrize("failure", [False, True])
def test_real_run_attempt_links_admission_and_records_caught_failure(
    recorded, monkeypatch, failure
) -> None:
    telemetry, exporter, logs = recorded
    repository = Repository()
    provider = Provider(failure)
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))

    async def exercise() -> None:
        runtime = service(telemetry, repository, provider)
        with telemetry.operation("websocket.command"):
            admitted = await runtime.create_run("session", "request", CANARY)
        await runtime.wait_for_run(admitted.id)
        await runtime.close()

    asyncio.run(exercise())
    spans = {span.name: span for span in exporter.get_finished_spans()}
    execution = spans["run.execute"]
    admission = spans["run.admit"]
    model = spans["model.call"]
    assert execution.parent is None
    assert execution.context.trace_id != admission.context.trace_id
    assert execution.links[0].context.span_id == admission.context.span_id
    assert model.parent.span_id == execution.context.span_id
    assert model.attributes["modelCallId"] == "owned-model-call"
    assert model.attributes["total_tokens"] == 110
    assert provider.closed
    assert repository.run.status is (RunStatus.FAILED if failure else RunStatus.COMPLETED)
    assert execution.attributes["outcome"] == ("failed" if failure else "completed")
    assert model.attributes["outcome"] == ("failed" if failure else "completed")
    if failure:
        assert execution.attributes["error_code"] == "provider_unavailable"
        assert execution.status.status_code.name == "ERROR"
    assert CANARY.encode() not in encode_spans(exporter.get_finished_spans()).SerializeToString()
    assert len(logs.get_finished_logs()) >= 4


def test_model_span_waits_for_cleanup_and_cancellation_is_not_error(recorded) -> None:
    telemetry, exporter, _logs = recorded
    cleanup = asyncio.Event()
    started = asyncio.Event()

    class WaitingProvider:
        async def stream(self, *args):
            try:
                started.set()
                await asyncio.Event().wait()
                yield ModelStreamEvent("completed")
            finally:
                cleanup.set()

    async def exercise() -> None:
        runtime = service(telemetry, Repository(), WaitingProvider())
        task = asyncio.create_task(
            runtime._stream_model_step(
                run_record(),
                model_record(),
                request(),
                cast(StreamingProviderAdapter, WaitingProvider()),
                CANARY,
                "user",
                0,
                0,
            )
        )
        await started.wait()
        assert not exporter.get_finished_spans()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup.is_set()

    asyncio.run(exercise())
    span = exporter.get_finished_spans()[0]
    assert span.attributes["outcome"] == "cancelled"
    assert span.status.status_code.name != "ERROR"


def test_tool_returned_error_is_an_error_span(recorded) -> None:
    telemetry, exporter, _logs = recorded
    runtime = service(telemetry, Repository(), Provider())
    runtime._tool_registry = Mock()
    runtime._tool_registry.execute = AsyncMock(
        return_value=ToolResult("provider-id", "tool", CANARY, True, "tool_unavailable")
    )
    stored = ToolCallRecord(
        "owned-tool-call",
        "owned-run",
        "message",
        0,
        "provider-id",
        "tool",
        {"private": CANARY},
        ToolCallStatus.PENDING,
        None,
        None,
        "now",
        None,
    )
    result = asyncio.run(
        runtime._execute_tool(
            run_record(), stored, ModelToolCall("provider-id", "tool", {"private": CANARY}), None, 5
        )
    )
    assert result.is_error
    span = exporter.get_finished_spans()[0]
    assert span.attributes["toolCallId"] == "owned-tool-call"
    assert span.attributes["error_code"] == "tool_unavailable"
    assert span.status.status_code.name == "ERROR"
    assert CANARY.encode() not in encode_spans([span]).SerializeToString()


def test_context_and_token_count_fallback_remain_visible(recorded, monkeypatch) -> None:
    telemetry, exporter, _logs = recorded
    repository = Repository()
    provider = Mock()
    provider.count_input = AsyncMock(side_effect=ProviderError("provider_unavailable", CANARY))
    context = RunContext(
        cast(RunRepository, repository),
        Mock(),
        Mock(),
        lambda _id, _calls: (0, 0),
        AsyncMock(),
        SecretRedactor(),
        telemetry=telemetry,
    )
    profile = profile_for_model(MODEL.id)
    assert profile is not None

    async def prepare(*args, **kwargs):
        return await context._preflight(
            run_record(),
            provider,
            CANARY,
            "user",
            request(),
            items=(ContextItem("input", "turn", ModelMessage("user", CANARY), required=True),),
            profile=profile,
            remaining_run_tokens=100000,
            remaining_run_cost_usd=10,
        )

    monkeypatch.setattr(context, "_prepare", prepare)
    result = asyncio.run(
        context.prepare(
            run_record(),
            MODEL,
            profile,
            provider,
            CANARY,
            "user",
            workspace_root=None,
            guidance=None,
            tools=(),
        )
    )
    assert result.count_method == "utf8_bytes_estimate_v1"
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["model.count_input"].attributes["outcome"] == "fallback"
    assert spans["model.count_input"].parent.span_id == spans["context.build"].context.span_id
    assert spans["context.build"].attributes["count_source"] == "utf8_bytes_estimate_v1"


def test_native_compaction_invalid_payload_is_failed_even_when_fallback_is_returned(
    recorded,
) -> None:
    telemetry, exporter, logs = recorded
    repository = Repository()
    provider = Mock()
    provider.compact = AsyncMock(
        return_value=NativeCompactionResult(
            "openai_window",
            None,
            (
                ModelStreamEvent(
                    "usage",
                    input_tokens=100,
                    output_tokens=10,
                    cached_tokens=0,
                    cache_creation_tokens=0,
                ),
            ),
            None,
            "native_context_invalid",
        )
    )
    context = RunContext(
        cast(RunRepository, repository),
        Mock(),
        Mock(),
        lambda _id, _calls: (0, 0),
        AsyncMock(),
        SecretRedactor(),
        telemetry=telemetry,
    )
    result = asyncio.run(context._compact(run_record(), MODEL, provider, plan(), CANARY, "user", 1))
    assert result is None
    span = exporter.get_finished_spans()[0]
    assert span.attributes["purpose"] == "native_compaction"
    assert span.attributes["outcome"] == "failed"
    assert span.attributes["modelCallId"] == "owned-model-call"
    assert repository.calls[0].status is ModelCallStatus.FAILED
    assert logs.get_finished_logs()[-1].log_record.attributes["outcome"] == "failed"


def test_portable_summary_has_its_own_correlated_model_call(recorded) -> None:
    telemetry, exporter, _logs = recorded
    repository = Repository()
    provider = Provider()
    context = RunContext(
        cast(RunRepository, repository),
        Mock(),
        Mock(),
        lambda _id, _calls: (0, 0),
        AsyncMock(),
        SecretRedactor(),
        telemetry=telemetry,
    )
    result, call = asyncio.run(
        context._summarize(
            run_record(),
            MODEL,
            cast(StreamingProviderAdapter, provider),
            plan(),
            CANARY,
            "user",
            1,
        )
    )
    assert result == "answer"
    assert call.status is ModelCallStatus.COMPLETED
    assert provider.closed
    span = exporter.get_finished_spans()[0]
    assert span.attributes["purpose"] == "summary"
    assert span.attributes["modelCallId"] == call.id
    assert span.attributes["total_tokens"] == 110
    assert span.attributes["outcome"] == "completed"


def test_approval_reschedule_links_a_new_root_attempt(recorded, monkeypatch) -> None:
    telemetry, exporter, _logs = recorded
    repository = Repository()
    repository.calls = [
        replace(
            model_record(),
            status=ModelCallStatus.COMPLETED,
            input_tokens=1,
            output_tokens=1,
            cached_tokens=0,
            cache_creation_tokens=0,
            estimated_cost=0.0001,
        )
    ]
    runtime = service(telemetry, repository, Provider())
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
    approvals = 0

    async def pending_tools(*args):
        nonlocal approvals
        approvals += 1
        if approvals == 1:
            repository.run = replace(repository.run, status=RunStatus.WAITING_FOR_APPROVAL)
            with telemetry.operation("tool.approval"):
                runtime._schedule(repository.run.id)
            return False
        return True

    monkeypatch.setattr(runtime, "_execute_pending_tools", pending_tools)

    async def exercise() -> None:
        with telemetry.operation("admission"):
            runtime._schedule(repository.run.id)
        for _ in range(100):
            if not runtime._tasks:
                break
            await asyncio.sleep(0.001)
        assert not runtime._tasks
        await runtime.close()

    asyncio.run(exercise())
    attempts = [span for span in exporter.get_finished_spans() if span.name == "run.execute"]
    approval = next(span for span in exporter.get_finished_spans() if span.name == "tool.approval")
    assert len(attempts) == 2
    assert attempts[0].attributes["outcome"] == "waiting_for_approval"
    assert attempts[1].attributes["outcome"] == "completed"
    assert attempts[1].parent is None
    assert attempts[1].links[0].context.span_id == approval.context.span_id
    assert attempts[0].context.trace_id != attempts[1].context.trace_id


def test_started_transition_failure_marks_the_attempt_failed(recorded, monkeypatch) -> None:
    telemetry, exporter, logs = recorded
    repository = Repository()
    monkeypatch.setattr(
        repository,
        "transition_run_record",
        AsyncMock(side_effect=ApplicationError("database_unavailable", CANARY)),
    )
    runtime = service(telemetry, repository, Provider())

    async def exercise() -> None:
        admitted = await runtime.create_run("session", "request", CANARY)
        await runtime.wait_for_run(admitted.id)

    asyncio.run(exercise())
    execution = next(span for span in exporter.get_finished_spans() if span.name == "run.execute")
    assert execution.attributes["outcome"] == "failed"
    assert execution.attributes["error_code"] == "database_unavailable"
    assert execution.status.status_code.name == "ERROR"
    assert repository.run.status is RunStatus.QUEUED
    assert logs.get_finished_logs()[-1].log_record.attributes["outcome"] == "failed"
