from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from run_fixtures import (
    CANARY,
    MODEL,
    Provider,
    Repository,
    model_record,
    plan,
    request,
    run_record,
    service,
)

from app.api.request_logging import RequestLoggingMiddleware, owned_chat_logging
from app.api.runtime_logging import runtime_command
from app.application.errors import ApplicationError, ProviderError
from app.application.model_catalog import profile_for_model
from app.application.ports import RunRepository, StreamingProviderAdapter
from app.application.run_context import RunContext
from app.application.secret_redaction import SecretRedactor
from app.domain.context import ContextItem, NativeCompactionResult
from app.domain.models import Session
from app.domain.runtime import ModelCallStatus, ModelMessage, ModelStreamEvent, RunStatus
from app.infrastructure.logging import JsonLogFormatter, configure_logging


def test_formatter_serializes_only_safe_structured_fields() -> None:
    record = logging.makeLogRecord(
        {
            "name": "app.runtime",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": CANARY,
            "args": (CANARY,),
            "created": datetime(2026, 10, 18, tzinfo=UTC).timestamp(),
            "event": "run.lifecycle",
            "outcome": "completed",
            "run_id": "c0000000-0000-4000-8000-000000000003",
            "turn_id": CANARY,
            "error_code": "provider_unavailable",
            "prompt": CANARY,
            "url": "https://private.example/path?token=secret",
            "workspace_path": "/private/workspace",
        }
    )

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload == {
        "timestamp": "2026-10-18T00:00:00.000Z",
        "level": "info",
        "logger": "app.runtime",
        "event": "run.lifecycle",
        "outcome": "completed",
        "error_code": "provider_unavailable",
        "run_id": "c0000000-0000-4000-8000-000000000003",
    }
    assert CANARY not in json.dumps(payload)


def test_formatter_rejects_unsafe_metadata_and_has_valid_failure_fallback() -> None:
    formatter = JsonLogFormatter()
    record = logging.makeLogRecord(
        {
            "name": "invalid logger name?",
            "levelno": logging.ERROR,
            "levelname": "ERROR",
            "msg": "private exception body",
            "created": datetime.now(UTC).timestamp(),
            "event": "http.request.completed",
            "route": "/api/chats/private-session-id?token=secret",
            "chat_id": "../private-session-id",
            "status_code": True,
            "duration_ms": float("nan"),
            "outcome": "private outcome",
        }
    )
    payload = json.loads(formatter.format(record))
    assert payload["logger"] == "application"
    assert payload["event"] == "http.request.completed"
    assert "route" not in payload
    assert "chat_id" not in payload
    assert "status_code" not in payload
    assert "duration_ms" not in payload
    assert "outcome" not in payload
    assert "private" not in json.dumps(payload)

    invalid_time = logging.makeLogRecord(
        {"name": "app", "levelno": logging.INFO, "levelname": "INFO", "created": float("inf")}
    )
    fallback = formatter.format(invalid_time)
    assert json.loads(fallback)["event"] == "log.format_failed"


def test_logging_configuration_is_idempotent_and_disables_raw_uvicorn_access() -> None:
    root = logging.getLogger()
    prior_handlers = root.handlers[:]
    access = logging.getLogger("uvicorn.access")
    prior_disabled = access.disabled
    try:
        configure_logging()
        configure_logging()
        marked = [
            handler for handler in root.handlers if getattr(handler, "trellis_json_handler", False)
        ]
        assert len(marked) == 1
        assert access.disabled
    finally:
        for handler in tuple(root.handlers):
            if getattr(handler, "trellis_json_handler", False):
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = prior_handlers
        access.disabled = prior_disabled


async def _invoke_middleware(scope: dict[str, object]) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def app(_scope, _receive, send) -> None:
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"private body"})

    async def send(message) -> None:
        messages.append(message)

    await RequestLoggingMiddleware(app)(scope, AsyncMock(), send)
    return messages


def test_http_logs_route_template_and_only_owned_chat_identifier(caplog) -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/chats/private-session-id",
        "route": SimpleNamespace(path="/api/chats/{chat_id}"),
        "headers": [(b"x-private-header", b"private-value")],
    }

    async def exercise() -> None:
        with owned_chat_logging(
            scope,
            Session("a0000000-0000-4000-8000-000000000001", "user", "title", "now", "now", 0),
        ):
            await _invoke_middleware(cast(dict[str, object], scope))

    caplog.set_level(logging.INFO)
    asyncio.run(exercise())
    record = next(record for record in caplog.records if record.name.endswith("request_logging"))
    payload = json.loads(JsonLogFormatter().format(record))
    assert record.levelno == logging.WARNING
    assert payload["route"] == "/api/chats/{chat_id}"
    assert payload["status_code"] == 403
    assert payload["chat_id"] == "a0000000-0000-4000-8000-000000000001"
    assert payload["outcome"] == "rejected"
    assert "private-session-id" not in json.dumps(payload)
    assert "private-value" not in json.dumps(payload)


def test_http_skips_health_and_cancellation_is_logged_at_info(caplog) -> None:
    async def exercise() -> None:
        async def health_app(_scope, _receive, _send) -> None:
            return None

        await RequestLoggingMiddleware(health_app)(
            {"type": "http", "method": "GET", "path": "/health"}, AsyncMock(), AsyncMock()
        )

        async def cancelled_app(_scope, _receive, _send) -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await RequestLoggingMiddleware(cancelled_app)(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/run",
                    "route": SimpleNamespace(path="/api/run"),
                },
                AsyncMock(),
                AsyncMock(),
            )

    caplog.set_level(logging.INFO)
    asyncio.run(exercise())
    records = [record for record in caplog.records if record.name.endswith("request_logging")]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert json.loads(JsonLogFormatter().format(records[0]))["outcome"] == "cancelled"


def test_runtime_command_logs_safe_outcome_and_suppresses_exception_text(caplog) -> None:
    caplog.set_level(logging.INFO)
    with runtime_command("run.start") as command:
        command.identify_run("c0000000-0000-4000-8000-000000000003")

    with pytest.raises(RuntimeError, match=CANARY), runtime_command("private.method"):
        raise RuntimeError(CANARY)

    with runtime_command("run.cancel") as command:
        command.outcome("rejected", "run_not_found")

    records = [record for record in caplog.records if record.name.endswith("runtime_logging")]
    complete = json.loads(JsonLogFormatter().format(records[0]))
    failed = json.loads(JsonLogFormatter().format(records[1]))
    assert complete["command"] == "run.start"
    assert complete["run_id"] == "c0000000-0000-4000-8000-000000000003"
    assert complete["outcome"] == "completed"
    assert failed["command"] == "unknown"
    assert failed["outcome"] == "error"
    rejected = json.loads(JsonLogFormatter().format(records[2]))
    assert failed["error_code"] == "command_failed"
    assert rejected["outcome"] == "rejected"
    assert rejected["error_code"] == "run_not_found"
    assert records[2].levelno == logging.WARNING
    assert CANARY not in json.dumps(failed)
    assert records[1].exc_info is None


def test_run_lifecycle_logs_success_and_failure_without_user_content(caplog, monkeypatch) -> None:
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        for failure in (False, True):
            repository = Repository()
            runtime = service(repository, Provider(failure))
            admitted = await runtime.create_run("session", "request", CANARY)
            await runtime.wait_for_run(admitted.id)
            await runtime.close()

    asyncio.run(exercise())
    events = [
        json.loads(JsonLogFormatter().format(record))
        for record in caplog.records
        if record.name.endswith("runs") and getattr(record, "event", None) == "run.lifecycle"
    ]
    assert [(event["level"], event["outcome"]) for event in events] == [
        ("info", "started"),
        ("info", "completed"),
        ("info", "started"),
        ("error", "failed"),
    ]
    assert all(event["run_id"] == "c0000000-0000-4000-8000-000000000003" for event in events)
    assert all(CANARY not in json.dumps(event) for event in events)


def test_native_compaction_invalid_payload_stays_failed_when_fallback_is_available(caplog) -> None:
    repository = Repository()
    provider = AsyncMock()
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
        AsyncMock(),
        AsyncMock(),
        lambda _model_id, _calls: (0, 0),
        AsyncMock(),
        SecretRedactor(),
    )

    caplog.set_level(logging.INFO)
    result = asyncio.run(
        context._compact(
            run_record(),
            MODEL,
            cast(StreamingProviderAdapter, provider),
            plan(),
            CANARY,
            "user",
            1,
        )
    )

    assert result is None
    assert repository.calls[0].status is ModelCallStatus.FAILED
    assert repository.calls[0].error_code == "native_context_invalid"
    assert repository.calls[0].accounted_total_tokens == 110
    fallback = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "context.native_compaction.fallback"
    )
    payload = json.loads(JsonLogFormatter().format(fallback))
    assert fallback.levelno == logging.WARNING
    assert payload["outcome"] == "fallback"
    assert payload["run_id"] == "c0000000-0000-4000-8000-000000000003"
    assert CANARY not in json.dumps(payload)


def test_token_count_provider_failure_warns_and_uses_estimate(caplog) -> None:
    repository = Repository()
    provider = Mock()
    provider.count_input = AsyncMock(side_effect=ProviderError("provider_unavailable", CANARY))
    context = RunContext(
        cast(RunRepository, repository),
        Mock(),
        Mock(),
        lambda _model_id, _calls: (0, 0),
        AsyncMock(),
        SecretRedactor(),
    )
    profile = profile_for_model(MODEL.id)
    assert profile is not None

    caplog.set_level(logging.INFO)
    result = asyncio.run(
        context._preflight(
            run_record(),
            provider,
            CANARY,
            "user",
            request(),
            items=(ContextItem("input", "turn", ModelMessage("user", CANARY), required=True),),
            profile=profile,
            remaining_run_tokens=100_000,
            remaining_run_cost_usd=10,
        )
    )

    fallback = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "context.token_count.fallback"
    )
    payload = json.loads(JsonLogFormatter().format(fallback))
    assert result.count_method == "utf8_bytes_estimate_v1"
    assert fallback.levelno == logging.WARNING
    assert payload["error_code"] == "provider_unavailable"
    assert payload["run_id"] == "c0000000-0000-4000-8000-000000000003"
    assert CANARY not in json.dumps(payload)


def test_started_transition_failure_leaves_queued_run_unstarted(caplog, monkeypatch) -> None:
    repository = Repository()
    monkeypatch.setattr(
        repository,
        "transition_run_record",
        AsyncMock(side_effect=ApplicationError("database_unavailable", CANARY)),
    )

    async def exercise() -> None:
        runtime = service(repository, Provider())
        admitted = await runtime.create_run("session", "request", CANARY)
        await runtime.wait_for_run(admitted.id)
        await runtime.close()

    caplog.set_level(logging.INFO)
    asyncio.run(exercise())

    assert repository.run.status is RunStatus.QUEUED
    assert not any(getattr(record, "event", None) == "run.lifecycle" for record in caplog.records)


def test_empty_provider_completion_fails_run_and_keeps_usage_accounting(monkeypatch) -> None:
    class EmptyProvider:
        async def stream(self, *args):
            yield ModelStreamEvent(
                "usage", input_tokens=100, output_tokens=0, cached_tokens=0, cache_creation_tokens=0
            )
            yield ModelStreamEvent("completed", finish_kind="final")

    repository = Repository()
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))

    async def exercise() -> None:
        runtime = service(repository, EmptyProvider())
        admitted = await runtime.create_run("session", "request", CANARY)
        await runtime.wait_for_run(admitted.id)
        await runtime.close()

    asyncio.run(exercise())

    assert repository.run.status is RunStatus.FAILED
    assert repository.calls[0].status is ModelCallStatus.FAILED
    assert repository.calls[0].input_tokens == 100
    assert repository.calls[0].output_tokens == 0


def test_legacy_turn_text_is_omitted_from_structured_run_logs(caplog, monkeypatch) -> None:
    repository = Repository()
    repository.run = replace(repository.run, turn_id=CANARY)
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))

    async def exercise() -> None:
        runtime = service(repository, Provider())
        admitted = await runtime.create_run("session", "request", CANARY)
        await runtime.wait_for_run(admitted.id)
        await runtime.close()

    caplog.set_level(logging.INFO)
    asyncio.run(exercise())

    payloads = [
        JsonLogFormatter().format(record)
        for record in caplog.records
        if record.name.endswith("runs")
    ]
    assert repository.run.status is RunStatus.COMPLETED
    assert payloads
    assert all(CANARY not in payload for payload in payloads)
    assert all("turn_id" not in json.loads(payload) for payload in payloads)


def test_approval_reschedule_restarts_run_after_pending_approval(monkeypatch) -> None:
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
    runtime = service(repository, Provider())
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
    approval_checks = 0

    async def pending_tools(*_args):
        nonlocal approval_checks
        approval_checks += 1
        if approval_checks == 1:
            repository.run = replace(repository.run, status=RunStatus.WAITING_FOR_APPROVAL)
            runtime._schedule(repository.run.id)
            return False
        return True

    monkeypatch.setattr(runtime, "_execute_pending_tools", pending_tools)

    async def exercise() -> None:
        runtime._schedule(repository.run.id)
        for _ in range(100):
            if not runtime._tasks:
                break
            await asyncio.sleep(0.001)
        assert not runtime._tasks
        await runtime.close()

    asyncio.run(exercise())

    assert approval_checks == 2
    assert repository.run.status is RunStatus.COMPLETED


def test_provider_cancellation_waits_for_stream_cleanup() -> None:
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
        repository = Repository()
        repository.run = replace(repository.run, status=RunStatus.RUNNING)
        runtime = service(repository, WaitingProvider())
        provider = WaitingProvider()
        task = asyncio.create_task(
            runtime._stream_model_step(
                repository.run,
                model_record(),
                request(),
                cast(StreamingProviderAdapter, provider),
                CANARY,
                "user",
                0,
                0,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup.is_set()

    asyncio.run(exercise())


def test_scheduled_runs_use_an_isolated_context_and_coalesce_reschedules(monkeypatch) -> None:
    marker = contextvars.ContextVar[str | None]("request_marker", default=None)
    marker.set("private request context")
    runtime = service(Repository(), Provider())
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    observed: list[object] = []
    executions = 0

    async def execute(_run_id: str) -> None:
        nonlocal executions
        executions += 1
        observed.append(marker.get())
        if executions == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()

    monkeypatch.setattr(runtime, "_run_with_limit", execute)
    monkeypatch.setattr(runtime, "_can_execute", lambda _run_id: True)

    async def exercise() -> None:
        runtime._schedule("owned-run")
        await first_started.wait()
        runtime._schedule("owned-run")
        runtime._schedule("owned-run")
        assert runtime._reschedule_pending == {"owned-run"}
        release_first.set()
        await second_started.wait()
        task = runtime._tasks["owned-run"]
        await task
        await asyncio.sleep(0)
        assert executions == 2
        assert observed == [None, None]

    asyncio.run(exercise())
