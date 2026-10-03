import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from app.application.errors import ApplicationError, ProviderError
from app.application.runs import RunService
from app.core.config import Settings
from app.domain.models import ModelDescriptor, ProviderName
from app.domain.runtime import (
    ModelRequest,
    ModelStreamEvent,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
)
from app.infrastructure.database import Database
from app.infrastructure.secrets import SecretStore


def make_settings(data_dir: Path) -> Settings:
    return Settings(environment="test", data_dir=data_dir)


class RecordingProvider:
    name: ProviderName = "openai"
    model = "test-model"

    def __init__(self, events: tuple[ModelStreamEvent, ...]) -> None:
        self.events = events
        self.request: ModelRequest | None = None
        self.api_key: str | None = None
        self.user_id: str | None = None

    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        self.api_key = api_key
        self.user_id = user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            for event in self.events:
                yield event

        return generate()

    async def complete(self, *_args: object, **_kwargs: object) -> str:
        raise NotImplementedError


class FailingProvider(RecordingProvider):
    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        self.api_key = api_key
        self.user_id = user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            yield ModelStreamEvent(kind="text_delta", text="partial")
            raise ProviderError("provider_upstream_failed", "Safe provider failure")

        return generate()


class TimedOutProvider(RecordingProvider):
    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        self.api_key = api_key
        self.user_id = user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            raise ProviderError("provider_timeout", "Safe timeout")
            yield ModelStreamEvent(kind="completed")

        return generate()


class BlockingProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__(())
        self.started = asyncio.Event()

    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        self.api_key = api_key
        self.user_id = user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            self.started.set()
            await asyncio.Event().wait()
            yield ModelStreamEvent(kind="completed")

        return generate()


class IncompleteProvider(RecordingProvider):
    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        self.api_key = api_key
        self.user_id = user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            yield ModelStreamEvent(kind="text_delta", text="truncated")

        return generate()


class UnexpectedFailureProvider(RecordingProvider):
    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        self.api_key = api_key
        self.user_id = user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            raise RuntimeError(f"unexpected provider failure: {api_key}")
            yield ModelStreamEvent(kind="completed")

        return generate()


class OneShotGetFailureDatabase(Database):
    fail_next_get_run = False

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        if self.fail_next_get_run:
            self.fail_next_get_run = False
            raise RuntimeError("temporary database read failure")
        return await super().get_run(run_id)


class SlowCreateRunDatabase(Database):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.create_started = asyncio.Event()
        self.allow_create = asyncio.Event()

    async def create_run(
        self,
        session_id: str,
        turn_id: str,
        client_request_id: str,
        content: str,
        model: ModelDescriptor,
    ) -> RunSnapshot:
        self.create_started.set()
        await self.allow_create.wait()
        return await super().create_run(
            session_id,
            turn_id,
            client_request_id,
            content,
            model,
        )


class PersistBeforePublish:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.events: list[RunEvent] = []

    async def publish(self, event: RunEvent) -> None:
        persisted = await self.database.list_run_events(
            event.run_id,
            after_sequence=event.sequence - 1,
            limit=1,
        )
        assert persisted and persisted[0] == event
        self.events.append(event)


def test_run_service_streams_and_persists_a_provider_neutral_run(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = RecordingProvider(
        (
            ModelStreamEvent(kind="text_delta", text="Hello"),
            ModelStreamEvent(
                kind="usage",
                input_tokens=5,
                output_tokens=1,
                cached_tokens=2,
                provider_response_id="response-1",
            ),
            ModelStreamEvent(
                kind="completed",
                finish_reason="stop",
                provider_response_id="response-1",
            ),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session()
        await secret_store.set("openai", "sk-runtime-secret")
        publisher = PersistBeforePublish(database)
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            publisher,
        )
        created = await service.create_run(
            session.id,
            "request-1",
            "Hello",
        )
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_messages(session.id),
            await database.list_run_events(created.id),
            await database.list_model_calls(created.id),
            publisher.events,
            (await database.get_profile()).id,
        )
        await service.close()
        return created, result

    created, result = asyncio.run(run())
    persisted, messages, events, model_calls, published, profile_id = result

    assert created.status is RunStatus.QUEUED
    assert persisted is not None
    assert persisted.status is RunStatus.COMPLETED
    assert persisted.stop_reason == "final_response"
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].content == "Hello"
    assert [event.event_type for event in events] == [
        RunEventType.QUEUED,
        RunEventType.STARTED,
        RunEventType.ASSISTANT_DELTA,
        RunEventType.MODEL_USAGE,
        RunEventType.MODEL_COMPLETED,
        RunEventType.ASSISTANT_COMPLETED,
        RunEventType.COMPLETED,
    ]
    assert published == events[1:]
    assert provider.request is not None
    assert provider.request.upstream_model_id == "gpt-5.5"
    assert provider.request.messages[0].content == "Hello"
    assert provider.api_key == "sk-runtime-secret"
    assert provider.user_id == profile_id
    assert len(model_calls) == 1
    assert model_calls[0].status.value == "completed"
    assert (model_calls[0].input_tokens, model_calls[0].output_tokens) == (5, 1)
    assert model_calls[0].cached_tokens == 2
    assert model_calls[0].provider_response_id == "response-1"
    assert b"sk-runtime-secret" not in settings.database_path.read_bytes()


def test_provider_failure_keeps_partial_events_but_no_assistant_message(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = FailingProvider(())

    async def run():
        await database.initialize()
        session = await database.create_session()
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
        )
        created = await service.create_run(session.id, "request-2", "Please answer")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_messages(session.id),
            await database.list_run_events(created.id),
            await database.list_model_calls(created.id),
        )
        await service.close()
        return result

    persisted, messages, events, model_calls = asyncio.run(run())

    assert persisted is not None
    assert persisted.status is RunStatus.FAILED
    assert persisted.error_code == "provider_upstream_failed"
    assert [message.role for message in messages] == ["user"]
    assert [event.event_type for event in events][-2:] == [
        RunEventType.ASSISTANT_DELTA,
        RunEventType.FAILED,
    ]
    assert model_calls[0].status.value == "failed"
    assert model_calls[0].error_message == "Safe provider failure"
    assert "sk-runtime-secret" not in str(events)


def test_run_service_requires_selected_streaming_adapter_before_persisting(
    tmp_path: Path,
) -> None:
    database = Database(make_settings(tmp_path).database_path)
    secret_store = SecretStore(make_settings(tmp_path).secrets_path)

    async def run() -> tuple[int, int]:
        await database.initialize()
        session = await database.create_session()
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {},
        )
        with pytest.raises(ApplicationError, match="not available"):
            await service.create_run(session.id, "request-3", "Hello")
        messages = await database.list_messages(session.id)
        runs = await database.list_run_events("missing-run")
        await service.close()
        return len(messages), len(runs)

    assert asyncio.run(run()) == (0, 0)


@pytest.mark.parametrize(
    ("provider", "expected_call_status", "expected_error"),
    [
        (TimedOutProvider(()), "timed_out", "provider_timeout"),
        (IncompleteProvider(()), "failed", "provider_invalid_response"),
    ],
)
def test_run_service_records_timeout_and_rejects_truncated_streams(
    tmp_path: Path,
    provider: RecordingProvider,
    expected_call_status: str,
    expected_error: str,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)

    async def run():
        await database.initialize()
        session = await database.create_session()
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
        )
        created = await service.create_run(session.id, "request-4", "Please answer")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_model_calls(created.id),
            await database.list_messages(session.id),
        )
        await service.close()
        return result

    persisted, calls, messages = asyncio.run(run())
    assert persisted is not None
    assert persisted.status is RunStatus.FAILED
    assert persisted.error_code == expected_error
    assert calls[0].status.value == expected_call_status
    assert [message.role for message in messages] == ["user"]


def test_shutdown_marks_running_and_semaphore_queued_runs_interrupted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = BlockingProvider()

    async def run():
        await database.initialize()
        first_session = await database.create_session()
        second_session = await database.create_session()
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            max_concurrent_runs=1,
        )
        first = await service.create_run(first_session.id, "request-5", "First")
        await provider.started.wait()
        second = await service.create_run(second_session.id, "request-6", "Second")
        await service.close()
        return await database.get_run(first.id), await database.get_run(second.id)

    first, second = asyncio.run(run())
    assert first is not None and first.status is RunStatus.INTERRUPTED
    assert second is not None and second.status is RunStatus.INTERRUPTED


def test_unexpected_provider_exception_does_not_expose_secret_in_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = UnexpectedFailureProvider(())

    async def run():
        await database.initialize()
        session = await database.create_session()
        await secret_store.set("openai", "sk-private-runtime-key")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
        )
        created = await service.create_run(session.id, "request-7", "Please answer")
        await service.wait_for_run(created.id)
        persisted = await database.get_run(created.id)
        await service.close()
        return persisted

    persisted = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.FAILED
    assert persisted.error_code == "runtime_internal_error"
    assert "sk-private-runtime-key" not in caplog.text


def test_prestart_repository_failure_is_persisted_without_unhandled_task_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings(tmp_path)
    database = OneShotGetFailureDatabase(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = RecordingProvider(())

    async def run():
        await database.initialize()
        session = await database.create_session()
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
        )
        database.fail_next_get_run = True
        created = await service.create_run(session.id, "request-8", "Please answer")
        await service.wait_for_run(created.id)
        persisted = await database.get_run(created.id)
        await service.close()
        return persisted

    persisted = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.FAILED
    assert persisted.error_code == "runtime_internal_error"
    assert "Traceback" not in caplog.text


def test_shutdown_waits_for_run_admission_before_draining_tasks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = SlowCreateRunDatabase(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = RecordingProvider((ModelStreamEvent(kind="text_delta", text="Will be interrupted"),))

    async def run():
        await database.initialize()
        session = await database.create_session()
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
        )
        admission = asyncio.create_task(
            service.create_run(session.id, "request-9", "Please answer")
        )
        await database.create_started.wait()
        shutdown = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not shutdown.done()
        database.allow_create.set()
        created = await admission
        await shutdown
        return await database.get_run(created.id)

    persisted = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.INTERRUPTED
