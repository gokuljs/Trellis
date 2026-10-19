import asyncio
import logging
import math
import re
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Literal
from urllib.parse import unquote

from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.application.secret_redaction import SecretRedactor
from app.application.telemetry import (
    NO_TELEMETRY,
    Attribute,
    Attributes,
    Operation,
    Telemetry,
    TraceLink,
)
from app.core.config import Settings

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_IDENTIFIERS = frozenset({"chatId", "turnId", "runId"})
_ATTRIBUTES = frozenset(
    {
        "chatId",
        "turnId",
        "runId",
        "modelCallId",
        "toolCallId",
        "operation",
        "outcome",
        "error_code",
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "rpc.method",
        "provider",
        "model",
        "purpose",
        "tool",
        "db.operation",
        "db.collection",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "total_tokens",
        "estimated_cost_usd",
        "max_tokens",
        "max_cost_usd",
        "deadline_seconds",
        "model_call_count",
        "tool_call_count",
        "context_tokens",
        "count_source",
        "streamed",
        "retryable",
        "duration_seconds",
        "queue_seconds",
        "stop_reason",
    }
)
_METRIC_ATTRIBUTES = frozenset(
    {
        "operation",
        "outcome",
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "rpc.method",
        "provider",
        "model",
        "purpose",
        "tool",
        "db.operation",
        "db.collection",
        "count_source",
        "stop_reason",
    }
)
_FAILED = frozenset({"failed", "error", "timeout", "interrupted", "rejected"})


class _ExporterLogFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.last_warning = float("-inf")

    def filter(self, record: logging.LogRecord) -> bool:
        now = time.monotonic()
        if now - self.last_warning < 60:
            return False
        self.last_warning = now
        # Exporter responses and exceptions may include remote content or credentials.
        record.msg = "Telemetry exporter reported a diagnostic."
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _safe_exporter_diagnostics() -> None:
    for name in (
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http._log_exporter",
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        "opentelemetry.exporter.otlp.common.http",
        "opentelemetry.exporter.otlp.proto.http._common",
        "opentelemetry.util.re",
        "opentelemetry.sdk._shared_internal",
        "opentelemetry.sdk.metrics._internal.export",
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _ExporterLogFilter) for item in logger.filters):
            logger.addFilter(_ExporterLogFilter())


def _span_context(link: TraceLink) -> trace.SpanContext:
    return trace.SpanContext(
        trace_id=link.trace_id,
        span_id=link.span_id,
        is_remote=link.remote,
        trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED if link.sampled else 0),
    )


def _link(span: trace.Span) -> TraceLink | None:
    context = span.get_span_context()
    return (
        TraceLink(context.trace_id, context.span_id, context.trace_flags.sampled, context.is_remote)
        if context.is_valid
        else None
    )


class _Operation:
    def __init__(self, runtime: TelemetryRuntime, span: trace.Span) -> None:
        self.runtime = runtime
        self.span = span
        self.has_outcome = False

    def set_attributes(self, attributes: Attributes) -> None:
        with suppress(Exception):
            self.span.set_attributes(self.runtime.correlated_attributes(attributes))

    def outcome(self, name: str, error_code: str | None = None) -> None:
        self.has_outcome = True
        self.set_attributes({"outcome": name, **({"error_code": error_code} if error_code else {})})
        with suppress(Exception):
            if name in _FAILED:
                self.span.set_status(trace.StatusCode.ERROR)

    def link(self) -> TraceLink | None:
        return _link(self.span)


class TelemetryRuntime:
    def __init__(
        self,
        tracer_provider: TracerProvider,
        logger_provider: LoggerProvider,
        meter_provider: MeterProvider,
        *,
        secrets: Sequence[str] = (),
    ) -> None:
        self.tracer_provider = tracer_provider
        self.logger_provider = logger_provider
        self.meter_provider = meter_provider
        self.tracer = tracer_provider.get_tracer("trellis")
        self.logger = logger_provider.get_logger("trellis")
        self.meter = meter_provider.get_meter("trellis")
        self.redactor = SecretRedactor(*secrets)
        self.counters: dict[str, Counter] = {}
        self.histograms: dict[str, Histogram] = {}
        self._correlation: ContextVar[dict[str, Attribute] | None] = ContextVar(
            "trellis.telemetry.correlation", default=None
        )
        self._closed = False

    def safe_attributes(
        self, attributes: Attributes | None, *, metric: bool = False
    ) -> dict[str, Attribute]:
        allowed = _METRIC_ATTRIBUTES if metric else _ATTRIBUTES
        result: dict[str, Attribute] = {}
        for key, value in (attributes or {}).items():
            if key not in allowed:
                continue
            if key in {"chatId", "turnId"}:
                if not isinstance(value, str) or not _UUID.fullmatch(value):
                    continue
                value = value.lower()
            if isinstance(value, str):
                if len(value) > 160 or self.redactor.redact_text(value) != value:
                    continue
                if any(ord(char) < 32 for char in value):
                    continue
            elif isinstance(value, float) and not math.isfinite(value):
                continue
            result[key] = value
        return result

    @contextmanager
    def correlate(self, attributes: Attributes) -> Iterator[None]:
        identifiers = {key: value for key, value in attributes.items() if key in _IDENTIFIERS}
        bound = {
            key: value
            for key, value in (self._correlation.get() or {}).items()
            if key not in identifiers
        }
        bound.update(self.safe_attributes(identifiers))
        token = self._correlation.set(bound)
        try:
            yield
        finally:
            self._correlation.reset(token)

    def correlated_attributes(self, attributes: Attributes | None) -> dict[str, Attribute]:
        return self.safe_attributes(attributes) | (self._correlation.get() or {})

    def _valid_name(self, name: str) -> bool:
        return bool(_NAME.fullmatch(name)) and self.redactor.redact_text(name) == name

    @contextmanager
    def operation(
        self,
        name: str,
        attributes: Attributes | None = None,
        *,
        root: bool = False,
        links: Sequence[TraceLink] = (),
        parent: TraceLink | None = None,
        kind: Literal["internal", "server", "client"] = "internal",
    ) -> Iterator[Operation]:
        if self._closed or not self._valid_name(name):
            yield NO_TELEMETRY
            return
        parent_context = (
            trace.set_span_in_context(trace.NonRecordingSpan(_span_context(parent)), Context())
            if parent is not None
            else Context()
            if root
            else None
        )
        try:
            span = self.tracer.start_span(
                name,
                context=parent_context,
                attributes=self.correlated_attributes(attributes),
                links=[trace.Link(_span_context(link)) for link in links],
                kind={
                    "internal": trace.SpanKind.INTERNAL,
                    "server": trace.SpanKind.SERVER,
                    "client": trace.SpanKind.CLIENT,
                }[kind],
            )
        except Exception:
            yield NO_TELEMETRY
            return
        operation = _Operation(self, span)
        try:
            with trace.use_span(span, record_exception=False, set_status_on_exception=False):
                try:
                    yield operation
                except BaseException as error:
                    if not operation.has_outcome:
                        operation.outcome(
                            "cancelled" if isinstance(error, asyncio.CancelledError) else "error",
                            "operation_cancelled"
                            if isinstance(error, asyncio.CancelledError)
                            else "operation_failed",
                        )
                    raise
        finally:
            with suppress(Exception):
                span.end()

    def capture(self) -> TraceLink | None:
        return _link(trace.get_current_span())

    def event(
        self, name: str, attributes: Attributes | None = None, *, error: bool = False
    ) -> None:
        if self._closed or not self._valid_name(name):
            return
        with suppress(Exception):
            self.logger.emit(
                body=name,
                event_name=name,
                severity_number=SeverityNumber.ERROR if error else SeverityNumber.INFO,
                attributes=self.correlated_attributes(attributes),
            )

    def count(self, name: str, value: int = 1, attributes: Attributes | None = None) -> None:
        if self._closed or not self._valid_name(name) or value < 0:
            return
        with suppress(Exception):
            counter = self.counters.get(name)
            if counter is None:
                counter = self.meter.create_counter(name)
                self.counters[name] = counter
            counter.add(value, self.safe_attributes(attributes, metric=True))

    def observe(self, name: str, value: float, attributes: Attributes | None = None) -> None:
        if self._closed or not self._valid_name(name) or not math.isfinite(value) or value < 0:
            return
        with suppress(Exception):
            histogram = self.histograms.get(name)
            if histogram is None:
                histogram = self.meter.create_histogram(name)
                self.histograms[name] = histogram
            histogram.record(value, self.safe_attributes(attributes, metric=True))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + 5
        with suppress(TimeoutError):
            async with asyncio.timeout(5):
                await asyncio.to_thread(self._shutdown, deadline)

    def _shutdown(self, deadline: float) -> None:
        def finish(provider: TracerProvider | LoggerProvider | MeterProvider) -> None:
            with suppress(Exception):
                provider.force_flush(
                    timeout_millis=max(1, int((deadline - time.monotonic()) * 1000))
                )
            with suppress(Exception):
                if isinstance(provider, MeterProvider):
                    provider.shutdown(timeout_millis=max(1, (deadline - time.monotonic()) * 1000))
                else:
                    provider.shutdown()

        workers = [
            threading.Thread(target=finish, args=(provider,), daemon=True)
            for provider in (self.tracer_provider, self.logger_provider, self.meter_provider)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))


def create_telemetry(settings: Settings) -> TelemetryRuntime | Telemetry:
    if not settings.telemetry_enabled:
        return NO_TELEMETRY
    console = settings.telemetry_exporter == "console"
    if not console:
        assert settings.telemetry_endpoint is not None
        assert settings.telemetry_headers is not None
    _safe_exporter_diagnostics()
    raw_headers = (
        settings.telemetry_headers.get_secret_value() if settings.telemetry_headers else ""
    )
    headers = {
        name.strip(): unquote(value.strip())
        for pair in raw_headers.split(",")
        for name, separator, value in [pair.partition("=")]
        if separator
    }
    secrets = [
        value.get_secret_value()
        for value in (settings.openai_api_key, settings.anthropic_api_key)
        if value is not None
    ]
    secrets.extend(headers.values())
    secrets.extend(value.removeprefix("Bearer ") for value in headers.values())
    resource = Resource(
        {
            "service.name": settings.telemetry_service_name,
            "service.version": settings.release,
            "deployment.environment.name": settings.environment,
        }
    )
    endpoint = settings.telemetry_endpoint.rstrip("/") if settings.telemetry_endpoint else ""
    cleanup: list[TracerProvider | LoggerProvider | PeriodicExportingMetricReader] = []
    try:
        tracer_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.telemetry_sample_ratio)),
            shutdown_on_exit=False,
            span_limits=SpanLimits(max_attributes=48, max_events=0, max_links=8),
        )
        cleanup.append(tracer_provider)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                ConsoleSpanExporter(out=sys.stdout)
                if console
                else OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers, timeout=3),
                max_queue_size=2048,
                max_export_batch_size=256,
                schedule_delay_millis=1000 if console else 5000,
                export_timeout_millis=3000,
            )
        )
        logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        cleanup.append(logger_provider)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                ConsoleLogRecordExporter(out=sys.stdout)
                if console
                else OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", headers=headers, timeout=3),
                max_queue_size=2048,
                max_export_batch_size=256,
                schedule_delay_millis=1000 if console else 5000,
                export_timeout_millis=3000,
            )
        )
        reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(out=sys.stdout)
            if console
            else OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers, timeout=3),
            export_interval_millis=5000 if console else 60000,
            export_timeout_millis=3000,
        )
        cleanup.append(reader)
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[reader],
            shutdown_on_exit=False,
        )
        return TelemetryRuntime(tracer_provider, logger_provider, meter_provider, secrets=secrets)
    except Exception:
        for provider in reversed(cleanup):
            with suppress(Exception):
                provider.shutdown()
        raise RuntimeError("Telemetry runtime initialization failed.") from None
