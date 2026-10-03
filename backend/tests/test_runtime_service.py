import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from app.application.errors import ApplicationError, ProviderError
from app.application.runs import RunService
from app.core.config import Settings
from app.domain.models import ProviderName
from app.domain.runtime import (
    ModelRequest,
    ModelStreamEvent,
    RunEvent,
    RunEventType,
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
