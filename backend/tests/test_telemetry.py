import asyncio
import logging
import threading
from collections.abc import Iterator
from unittest.mock import Mock

import httpx
import pytest
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
from pydantic import SecretStr, ValidationError

from app.application.telemetry import NO_TELEMETRY, TraceLink
from app.core.config import Settings
from app.infrastructure import telemetry as implementation
from app.infrastructure.telemetry import TelemetryRuntime, create_telemetry
from app.main import create_app

CANARY = "synthetic-private-provider-key-0123456789"


@pytest.fixture
def recorded() -> Iterator[
    tuple[TelemetryRuntime, InMemorySpanExporter, InMemoryLogRecordExporter, InMemoryMetricReader]
]:
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    reader = InMemoryMetricReader()
    tracer = TracerProvider(shutdown_on_exit=False)
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    logger = LoggerProvider(shutdown_on_exit=False)
    logger.add_log_record_processor(SimpleLogRecordProcessor(logs))
    meter = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    runtime = TelemetryRuntime(tracer, logger, meter, secrets=[CANARY])
    yield runtime, spans, logs, reader
    asyncio.run(runtime.close())


def test_disabled_runtime_has_no_exporters_threads_or_global_side_effects() -> None:
    threads = set(threading.enumerate())
    provider = trace.get_tracer_provider()
    telemetry = create_telemetry(Settings(_env_file=None))
    assert telemetry is NO_TELEMETRY
    assert set(threading.enumerate()) == threads
    assert trace.get_tracer_provider() is provider
    with telemetry.operation("run.execute", root=True, parent=TraceLink(1, 2)) as operation:
        operation.set_attributes({"runId": "a"})
        operation.outcome("failed", "safe_code")
        assert operation.link() is None
    telemetry.event("run.failed", error=True)
    telemetry.count("runs", 1)
    telemetry.observe("duration", 1)
    assert telemetry.capture() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"telemetry_endpoint": None},
        {"telemetry_headers": None},
        {"telemetry_endpoint": "http://remote.example.com"},
        {"telemetry_endpoint": f"https://{CANARY}@example.com"},
        {"telemetry_endpoint": f"https://example.com/?key={CANARY}"},
        {"telemetry_endpoint": "https://example.com:bad"},
        {"telemetry_headers": SecretStr(f"Authorization={CANARY}\n")},
        {"telemetry_headers": SecretStr("missing-separator")},
        {"telemetry_headers": SecretStr("Authorization=Bearer%0Asecret")},
        {"telemetry_sample_ratio": 1.1},
    ],
)
def test_invalid_enabled_settings_have_safe_errors(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "telemetry_enabled": True,
        "telemetry_endpoint": "https://example.com",
        "telemetry_headers": SecretStr("Authorization=Bearer%20test-token"),
    }
    values.update(changes)
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate(values)
    assert CANARY not in str(raised.value)


@pytest.mark.parametrize("headers", [f"Authorization={CANARY}", f"Authorization=Bearer%20{CANARY}"])
def test_standard_environment_aliases_and_secret_representation(
    monkeypatch: pytest.MonkeyPatch,
    headers: str,
) -> None:
    monkeypatch.setenv("TRELLIS_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.com")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", headers)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "trellis-test")
    settings = Settings(_env_file=None)
    assert settings.telemetry_endpoint == "https://example.com"
    assert settings.telemetry_service_name == "trellis-test"
    assert settings.telemetry_headers is not None
    assert settings.telemetry_headers.get_secret_value() == headers
    assert CANARY not in repr(settings)
    Settings(
        _env_file=None,
        telemetry_enabled=True,
        telemetry_endpoint="http://localhost:4318",
        telemetry_headers=SecretStr("Authorization=test"),
        environment="test",
    )


def test_nested_spans_logs_and_linked_attempts_share_business_correlation(recorded) -> None:
    runtime, exporter, logs, _ = recorded
    assert runtime.capture() is None
    with runtime.operation("run.admit", {"runId": "run-1"}) as admission:
        admission_link = admission.link()
        assert admission_link == runtime.capture()
        runtime.event("run.admitted", {"runId": "run-1"})
        with runtime.operation("db.read"):
            pass
    assert admission_link is not None
    with (
        runtime.operation("run.execute", {"runId": "run-1"}, root=True, links=[admission_link]),
        runtime.operation("model.call", {"modelCallId": "call-1"}),
    ):
        pass
    with runtime.operation("http.request", parent=TraceLink(1234, 5678, remote=True)):
        pass
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["db.read"].parent.span_id == spans["run.admit"].context.span_id
    assert spans["run.execute"].parent is None
    assert spans["run.execute"].context.trace_id != spans["run.admit"].context.trace_id
    assert spans["run.execute"].links[0].context.trace_id == spans["run.admit"].context.trace_id
    assert spans["model.call"].parent.span_id == spans["run.execute"].context.span_id
    assert spans["http.request"].context.trace_id == 1234
    assert spans["http.request"].parent.span_id == 5678
    assert logs.get_finished_logs()[0].log_record.trace_id == spans["run.admit"].context.trace_id


def test_exception_and_cancellation_preserve_outcomes_without_exception_content(recorded) -> None:
    runtime, exporter, _, _ = recorded
    with pytest.raises(RuntimeError, match=CANARY), runtime.operation("model.call"):
        raise RuntimeError(CANARY)
    with pytest.raises(asyncio.CancelledError), runtime.operation("run.execute"):
        raise asyncio.CancelledError(CANARY)
    with pytest.raises(TimeoutError), runtime.operation("tool.execute") as operation:
        operation.outcome("timeout", "tool_timeout")
        raise TimeoutError(CANARY)
    spans = exporter.get_finished_spans()
    assert [span.attributes["outcome"] for span in spans] == ["error", "cancelled", "timeout"]
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert spans[2].status.status_code == trace.StatusCode.ERROR
    assert all(not span.events and span.status.description is None for span in spans)
    assert CANARY.encode() not in encode_spans(spans).SerializeToString()


def test_export_boundary_filters_private_content_and_high_cardinality_metric_labels(
    recorded,
) -> None:
    runtime, spans, logs, reader = recorded
    attributes = {
        "runId": "run-1",
        "modelCallId": "call-1",
        "toolCallId": "tool-1",
        "provider": "openai",
        "model": CANARY,
        "purpose": "sk-proj-syntheticsecret12345",
        "tool": "a" * 161,
        "outcome": "ok\n",
        "total_tokens": 50,
        "estimated_cost_usd": float("nan"),
        "prompt": CANARY,
        "authorization": CANARY,
    }
    with runtime.operation("run.execute", attributes) as operation:
        operation.set_attributes({"outcome": "completed", "output_tokens": 20})
        operation.outcome("completed")
        runtime.event("run.completed", attributes, error=True)
        for _ in range(2):
            runtime.count("trellis.runs", attributes=attributes)
            runtime.observe("trellis.duration", 2, attributes)
    metrics = reader.get_metrics_data()
    assert metrics is not None
    emitted = spans.get_finished_spans()[0]
    assert emitted.attributes == {
        "runId": "run-1",
        "modelCallId": "call-1",
        "toolCallId": "tool-1",
        "provider": "openai",
        "total_tokens": 50,
        "outcome": "completed",
        "output_tokens": 20,
    }
    for metric in metrics.resource_metrics[0].scope_metrics[0].metrics:
        assert metric.data.data_points[0].attributes == {"provider": "openai"}
    payloads = (
        encode_spans(spans.get_finished_spans()).SerializeToString(),
        encode_logs(logs.get_finished_logs()).SerializeToString(),
        encode_metrics(metrics).SerializeToString(),
    )
    assert all(CANARY.encode() not in payload for payload in payloads)


def test_invalid_names_measurements_and_closed_runtime_are_noops(recorded) -> None:
    runtime, spans, logs, reader = recorded
    for name in ("raw content", CANARY, "sk-proj-syntheticsecret12345"):
        with runtime.operation(name) as operation:
            assert operation is NO_TELEMETRY
        runtime.event(name)
        runtime.count(name)
        runtime.observe(name, 1)
    runtime.count("negative", -1)
    runtime.observe("negative", -1)
    runtime.observe("nan", float("nan"))
    assert not spans.get_finished_spans()
    assert not logs.get_finished_logs()
    assert reader.get_metrics_data() is None
    asyncio.run(runtime.close())
    asyncio.run(runtime.close())
    with runtime.operation("after.close") as operation:
        assert operation is NO_TELEMETRY
    runtime.event("after.close")
    runtime.count("after.close")
    runtime.observe("after.close", 1)


def test_telemetry_failures_do_not_change_application_results(recorded, monkeypatch) -> None:
    runtime, _, _, _ = recorded
    monkeypatch.setattr(runtime.tracer, "start_span", Mock(side_effect=RuntimeError(CANARY)))
    with runtime.operation("run.execute") as operation:
        assert operation is NO_TELEMETRY
    monkeypatch.setattr(runtime.logger, "emit", Mock(side_effect=RuntimeError(CANARY)))
    monkeypatch.setattr(runtime.meter, "create_counter", Mock(side_effect=RuntimeError(CANARY)))
    monkeypatch.setattr(runtime.meter, "create_histogram", Mock(side_effect=RuntimeError(CANARY)))
    runtime.event("run.failed")
    runtime.count("trellis.runs")
    runtime.observe("trellis.duration", 1)
    for provider in (runtime.tracer_provider, runtime.logger_provider, runtime.meter_provider):
        monkeypatch.setattr(provider, "force_flush", Mock(side_effect=RuntimeError(CANARY)))
    asyncio.run(runtime.close())


def test_enabled_factory_uses_bounded_otlp_exporters_without_global_providers(monkeypatch) -> None:
    constructors = []
    for name in ("OTLPSpanExporter", "OTLPLogExporter", "OTLPMetricExporter"):
        original = getattr(implementation, name)
        wrapper = Mock(wraps=original)
        constructors.append(wrapper)
        monkeypatch.setattr(implementation, name, wrapper)
    provider = trace.get_tracer_provider()
    runtime = create_telemetry(
        Settings(
            _env_file=None,
            telemetry_enabled=True,
            telemetry_endpoint="https://example.com/otlp/",
            telemetry_headers=SecretStr(f"Authorization=Bearer%20{CANARY}"),
            openai_api_key=SecretStr(CANARY),
        )
    )
    assert isinstance(runtime, TelemetryRuntime)
    assert trace.get_tracer_provider() is provider
    for constructor, signal in zip(constructors, ("traces", "logs", "metrics"), strict=True):
        assert constructor.call_args.kwargs == {
            "endpoint": f"https://example.com/otlp/v1/{signal}",
            "headers": {"Authorization": f"Bearer {CANARY}"},
            "timeout": 3,
        }
    assert runtime.safe_attributes({"model": CANARY}) == {}
    asyncio.run(runtime.close())


def test_exporter_diagnostics_are_redacted_and_rate_limited(caplog) -> None:
    implementation._safe_exporter_diagnostics()
    logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    logger.error("Remote failure %s", CANARY, exc_info=RuntimeError(CANARY))
    logger.error("Second failure %s", CANARY)
    assert len(caplog.records) == 1
    assert caplog.records[0].message == "Telemetry exporter reported a diagnostic."
    assert CANARY not in caplog.text


def test_partial_startup_closes_existing_providers_and_hides_errors(monkeypatch) -> None:
    providers = []
    for name in ("TracerProvider", "LoggerProvider"):
        original = getattr(implementation, name)

        def construct(*args, _original=original, **kwargs):
            instance = _original(*args, **kwargs)
            instance.shutdown = Mock(wraps=instance.shutdown)
            providers.append(instance)
            return instance

        monkeypatch.setattr(implementation, name, construct)
    monkeypatch.setattr(implementation, "OTLPMetricExporter", Mock(side_effect=ValueError(CANARY)))
    with pytest.raises(RuntimeError, match="Telemetry runtime initialization failed") as raised:
        create_telemetry(
            Settings(
                _env_file=None,
                telemetry_enabled=True,
                telemetry_endpoint="https://example.com",
                telemetry_headers=SecretStr(f"Authorization=Bearer%20{CANARY}"),
            )
        )
    assert CANARY not in str(raised.value)
    assert len(providers) == 2
    assert all(provider.shutdown.call_count == 1 for provider in providers)


def test_application_owns_runtime_after_services_close(monkeypatch) -> None:
    runtime = Mock(spec=TelemetryRuntime)
    runtime.close = Mock(side_effect=lambda: asyncio.sleep(0))
    monkeypatch.setattr("app.main.create_telemetry", Mock(return_value=runtime))
    app = create_app(Settings(_env_file=None))

    async def exercise(application, *, owns_runtime):
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client,
        ):
            assert (await client.get("/health")).status_code == 200
            if owns_runtime:
                assert application.state.telemetry is runtime
                assert not runtime.close.called

    asyncio.run(exercise(app, owns_runtime=True))
    assert runtime.close.call_count == 1
    asyncio.run(
        exercise(create_app(Settings(_env_file=None), telemetry=NO_TELEMETRY), owns_runtime=False)
    )
