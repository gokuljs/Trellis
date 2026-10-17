import asyncio
import io
import json
import threading
from typing import Literal
from unittest.mock import Mock
from uuid import UUID

import pytest
from opentelemetry import trace
from pydantic import SecretStr, ValidationError

from app.application.telemetry import NO_TELEMETRY
from app.core.config import Settings
from app.infrastructure import telemetry as implementation
from app.infrastructure.telemetry import TelemetryRuntime, create_telemetry

PROVIDER_CANARY = "synthetic-private-provider-0123456789"
EXPORT_CANARY = "synthetic-private-export-0123456789"


def _console_settings(
    *,
    environment: Literal["development", "test", "preview", "production"] = "development",
    telemetry_enabled: bool = True,
    telemetry_headers: SecretStr | None = None,
    openai_api_key: SecretStr | None = None,
    web_origin: str | None = None,
    private_user_ids: frozenset[UUID] = frozenset(),
) -> Settings:
    return Settings(
        _env_file=None,
        telemetry_enabled=telemetry_enabled,
        telemetry_exporter="console",
        telemetry_endpoint=None,
        telemetry_headers=telemetry_headers,
        environment=environment,
        openai_api_key=openai_api_key,
        web_origin=web_origin,
        private_user_ids=private_user_ids,
    )


def _records(output: str) -> list[dict]:
    decoder = json.JSONDecoder()
    records = []
    remaining = output.strip()
    while remaining:
        record, end = decoder.raw_decode(remaining)
        records.append(record)
        remaining = remaining[end:].strip()
    return records


@pytest.mark.parametrize("environment", ["development", "test"])
def test_console_settings_require_no_otlp_credentials(
    environment: Literal["development", "test"],
) -> None:
    settings = _console_settings(environment=environment)
    assert settings.telemetry_exporter == "console"
    assert settings.telemetry_endpoint is None
    assert settings.telemetry_headers is None


@pytest.mark.parametrize("environment", ["preview", "production"])
def test_hosted_environments_reject_enabled_console_export(
    environment: Literal["preview", "production"],
) -> None:
    with pytest.raises(ValidationError, match="Console telemetry is only available"):
        _console_settings(
            environment=environment,
            web_origin="https://app.example.com",
            private_user_ids=frozenset({UUID(int=1)}),
        )


def test_disabled_console_export_has_no_output_threads_or_global_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    threads = set(threading.enumerate())
    provider = trace.get_tracer_provider()
    runtime = create_telemetry(_console_settings(telemetry_enabled=False))
    assert runtime is NO_TELEMETRY
    with runtime.operation("run.execute"):
        runtime.event("run.started")
        runtime.count("trellis.runs")
    assert output.getvalue() == ""
    assert set(threading.enumerate()) == threads
    assert trace.get_tracer_provider() is provider


@pytest.mark.parametrize("configured_headers", [False, True])
def test_console_payloads_correlate_and_redact_before_shutdown_flush(
    monkeypatch: pytest.MonkeyPatch, configured_headers: bool
) -> None:
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    for name in ("OTLPSpanExporter", "OTLPLogExporter", "OTLPMetricExporter"):
        monkeypatch.setattr(implementation, name, Mock(side_effect=AssertionError(name)))
    provider = trace.get_tracer_provider()
    runtime = create_telemetry(
        _console_settings(
            openai_api_key=SecretStr(PROVIDER_CANARY),
            telemetry_headers=SecretStr(f"Authorization=Bearer%20{EXPORT_CANARY}")
            if configured_headers
            else None,
        )
    )
    assert isinstance(runtime, TelemetryRuntime)
    assert trace.get_tracer_provider() is provider
    attributes = {
        "runId": "run-console-1",
        "provider": "openai",
        "model": PROVIDER_CANARY,
        "purpose": EXPORT_CANARY if configured_headers else PROVIDER_CANARY,
        "prompt": PROVIDER_CANARY,
    }
    try:
        with runtime.operation("run.execute", attributes) as run:
            with runtime.operation("model.call", {"modelCallId": "model-console-1"}) as model:
                model.outcome("completed")
                runtime.event("model.completed", attributes)
                runtime.count("trellis.model.calls", attributes=attributes)
                runtime.observe("trellis.model.duration", 0.125, attributes)
            run.outcome("completed")
    finally:
        asyncio.run(runtime.close())
    payload = output.getvalue()
    assert PROVIDER_CANARY not in payload
    if configured_headers:
        assert EXPORT_CANARY not in payload
    records = _records(payload)
    spans = {record["name"]: record for record in records if "context" in record}
    run_span = spans["run.execute"]
    model_span = spans["model.call"]
    assert run_span["attributes"]["runId"] == "run-console-1"
    assert "model" not in run_span["attributes"]
    assert model_span["context"]["trace_id"] == run_span["context"]["trace_id"]
    assert model_span["parent_id"] == run_span["context"]["span_id"]
    assert model_span["attributes"]["modelCallId"] == "model-console-1"
    assert model_span["attributes"]["outcome"] == "completed"
    log = next(record for record in records if record.get("body") == "model.completed")
    assert log["trace_id"] == model_span["context"]["trace_id"]
    assert log["span_id"] == model_span["context"]["span_id"]
    metrics = {
        metric["name"]: metric
        for record in records
        for resource in record.get("resource_metrics", [])
        for scope in resource["scope_metrics"]
        for metric in scope["metrics"]
    }
    assert metrics["trellis.model.calls"]["data"]["data_points"][0]["value"] == 1
    duration = metrics["trellis.model.duration"]["data"]["data_points"][0]
    assert duration["count"] == 1
    assert duration["sum"] == 0.125
    for metric in metrics.values():
        assert metric["data"]["data_points"][0]["attributes"] == {"provider": "openai"}


class _LiveOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.signals = {name: threading.Event() for name in ("span", "log", "metric")}
        self.writer_threads: set[int] = set()

    def write(self, value: str) -> int:
        written = super().write(value)
        self.writer_threads.add(threading.get_ident())
        for signal, marker in (
            ("span", '"name": "run.execute"'),
            ("log", '"body": "run.started"'),
            ("metric", '"name": "trellis.runs"'),
        ):
            if marker in value:
                self.signals[signal].set()
        return written


def test_console_exports_all_signals_in_background_before_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _LiveOutput()
    monkeypatch.setattr("sys.stdout", output)
    runtime = create_telemetry(_console_settings())
    assert isinstance(runtime, TelemetryRuntime)
    try:
        with runtime.operation("run.execute"):
            runtime.event("run.started")
            runtime.count("trellis.runs")
        for signal, arrived in output.signals.items():
            assert arrived.wait(timeout=8), f"No live console {signal} output"
        assert threading.get_ident() not in output.writer_threads
    finally:
        asyncio.run(runtime.close())
