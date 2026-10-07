import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.application.errors import ApplicationError, ProviderError
from app.application.runs import RunService
from app.application.tools import ToolRegistry
from app.core.config import Settings
from app.domain.models import ModelDescriptor, ProviderName
from app.domain.runtime import (
    ModelContinuationItem,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
    ToolCallStatus,
)
from app.infrastructure.database import Database
from app.infrastructure.local_tools import LocalReadToolExecutor, read_workspace_guidance
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


class SequencedProvider(RecordingProvider):
    def __init__(self, rounds: tuple[tuple[ModelStreamEvent, ...], ...]) -> None:
        super().__init__(())
        self.rounds = rounds
        self.requests: list[ModelRequest] = []

    def stream(
        self, request: ModelRequest, api_key: str, user_id: str
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.requests.append(request)
        round_index = len(self.requests) - 1

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            for event in self.rounds[round_index]:
                yield event

        return generate()


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
                kind="continuation_item",
                continuation_item=ModelContinuationItem(
                    "openai", "openai:gpt-5.5", '{"encrypted_content":"opaque"}'
                ),
            ),
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
        RunEventType.MODEL_USAGE,
        RunEventType.MODEL_COMPLETED,
        RunEventType.ASSISTANT_DELTA,
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
    assert "agent-foundation-v1" in model_calls[0].request_snapshot["system_instructions"]
    assert "No workspace is attached" in model_calls[0].request_snapshot["system_instructions"]
    assert model_calls[0].request_snapshot["tools"] == []
    assert model_calls[0].request_snapshot["messages"] == [
        {"role": "user", "content": "Hello", "tool_calls": [], "continuation_items": []}
    ]
    assert model_calls[0].response_snapshot is not None
    assert model_calls[0].response_snapshot["content"] == "Hello"
    assert model_calls[0].response_snapshot["tool_calls"] == []
    assert model_calls[0].response_snapshot["continuation_items"] == [
        {
            "provider_id": "openai",
            "model_id": "openai:gpt-5.5",
            "payload_json": '{"encrypted_content":"opaque"}',
        }
    ]
    assert model_calls[0].response_snapshot["usage"] == {
        "input_tokens": 5,
        "output_tokens": 1,
        "reasoning_tokens": None,
        "cached_tokens": 2,
    }
    assert b"sk-runtime-secret" not in settings.database_path.read_bytes()


def test_run_service_persists_read_tool_exchange_before_a_second_model_call(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    (tmp_path / "README.md").write_text("Hello from the workspace.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Read carefully.\n", encoding="utf-8")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(kind="text_delta", text="Checking the file."),
                ModelStreamEvent(
                    kind="continuation_item",
                    continuation_item=ModelContinuationItem(
                        "openai", "openai:gpt-5.5", '{"encrypted_content":"opaque"}'
                    ),
                ),
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-call-1", "read_file", {"path": "README.md"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="The README says hello."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(LocalReadToolExecutor()),
            workspace_guidance_reader=read_workspace_guidance,
        )
        created = await service.create_run(session.id, "request-tools", "What does README say?")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_messages(session.id),
            await database.list_model_calls(created.id),
            await database.list_run_messages(created.id),
            await database.list_tool_calls(created.id),
            await database.list_run_events(created.id),
        )
        await service.close()
        return result

    persisted, visible, calls, exchange, tool_calls, events = asyncio.run(run())

    assert persisted is not None and persisted.status is RunStatus.COMPLETED
    assert [message.content for message in visible] == [
        "What does README say?",
        "The README says hello.",
    ]
    assert len(calls) == 2 and [call.step_index for call in calls] == [1, 2]
    assert [message.role for message in exchange] == ["assistant", "tool"]
    assert exchange[0].content == "Checking the file."
    assert "1: Hello from the workspace." in exchange[1].content
    assert len(tool_calls) == 1 and tool_calls[0].status.value == "completed"
    assert len(provider.requests) == 2
    assert {tool.name for tool in provider.requests[0].tools} == {
        "list_files",
        "search_files",
        "read_file",
        "inspect_git",
    }
    assert "Read carefully." in provider.requests[0].system_instructions
    assert [message.role for message in provider.requests[1].messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert provider.requests[1].messages[-1].tool_call_id == "provider-call-1"
    assert provider.requests[1].messages[-2].continuation_items == (
        ModelContinuationItem("openai", "openai:gpt-5.5", '{"encrypted_content":"opaque"}'),
    )
    event_types = [event.event_type for event in events]
    assert event_types.index(RunEventType.TOOL_RESULT) < event_types.index(
        RunEventType.ASSISTANT_COMPLETED
    )
    assert [
        event.data["text"] for event in events if event.event_type is RunEventType.ASSISTANT_DELTA
    ] == ["The README says hello."]


def test_tool_only_response_records_two_results_before_continuing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    (tmp_path / "one.txt").write_text("first\n", encoding="utf-8")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("call-good", "read_file", {"path": "one.txt"}),
                ),
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("call-bad", "read_file", {"path": "../outside"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="I checked both files."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(LocalReadToolExecutor()),
        )
        created = await service.create_run(session.id, "request-two-tools", "Inspect two files")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_run_messages(created.id),
            await database.list_tool_calls(created.id),
            await database.list_messages(session.id),
        )
        await service.close()
        return result

    persisted, exchange, calls, visible = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.COMPLETED
    assert [message.role for message in exchange] == ["assistant", "tool", "tool"]
    assert [call.status for call in calls] == [ToolCallStatus.COMPLETED, ToolCallStatus.FAILED]
    assert "first" in exchange[1].content
    assert "path_not_allowed" in exchange[2].content
    assert [message.role for message in provider.requests[1].messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert [message.tool_call_id for message in provider.requests[1].messages[-2:]] == [
        "call-good",
        "call-bad",
    ]
    assert [message.content for message in visible] == [
        "Inspect two files",
        "I checked both files.",
    ]


def test_agent_loop_rejects_sensitive_tool_arguments_before_recording_them(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall(
                        "call-sensitive",
                        "search_files",
                        {"query": "OPENAI_API_KEY=sk-secret123"},
                    ),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="I cannot use that query."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(LocalReadToolExecutor()),
        )
        created = await service.create_run(session.id, "request-sensitive", "Inspect workspace")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_run_messages(created.id),
            await database.list_tool_calls(created.id),
            await database.list_model_calls(created.id),
            await database.list_run_events(created.id),
        )
        await service.close()
        return result

    persisted, exchange, calls, model_calls, events = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.COMPLETED
    assert calls[0].status is ToolCallStatus.FAILED
    assert "sensitive_tool_arguments" in exchange[1].content
    assert "sk-secret123" not in str(model_calls) + str(events)
    assert b"sk-secret123" not in settings.database_path.read_bytes()


def test_streamed_secret_split_across_chunks_is_redacted_before_persistence(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = RecordingProvider(
        (
            ModelStreamEvent(kind="text_delta", text="Authorization: Bearer "),
            ModelStreamEvent(kind="text_delta", text="secret123456"),
            ModelStreamEvent(kind="completed", finish_reason="stop"),
        )
    )

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
        created = await service.create_run(session.id, "request-split-secret", "Say hello")
        await service.wait_for_run(created.id)
        result = (
            await database.list_run_events(created.id),
            await database.list_messages(session.id),
        )
        await service.close()
        return result

    events, visible = asyncio.run(run())
    assert "secret123456" not in str(events) + str(visible)
    assert b"secret123456" not in settings.database_path.read_bytes()
    assert "[REDACTED]" in visible[-1].content


def test_cancelling_during_a_tool_records_a_terminal_result(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("call-blocked", "read_file", {"path": "note.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
        )
    )

    class BlockingExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path
        ) -> tuple[str, bool]:
            self.started.set()
            await asyncio.Event().wait()
            return "unreachable", False

    executor = BlockingExecutor()

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(executor),
        )
        created = await service.create_run(session.id, "request-cancel-tool", "Read note")
        await executor.started.wait()
        await service.cancel_run(created.id)
        await asyncio.gather(service._tasks[created.id], return_exceptions=True)
        result = (
            await database.get_run(created.id),
            await database.list_tool_calls(created.id),
            await database.list_run_messages(created.id),
            await database.list_run_events(created.id),
        )
        await service.close()
        return result

    persisted, calls, exchange, events = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.CANCELLED
    assert calls[0].status is ToolCallStatus.CANCELLED
    assert [message.role for message in exchange] == ["assistant", "tool"]
    assert events[-2].event_type is RunEventType.TOOL_RESULT
    assert events[-1].event_type is RunEventType.CANCELLED


def test_a_tool_cannot_run_past_the_run_deadline(tmp_path: Path) -> None:
    class NearDeadlineDatabase(Database):
        async def create_run(
            self,
            session_id: str,
            turn_id: str,
            client_request_id: str,
            content: str,
            model: ModelDescriptor,
        ) -> RunSnapshot:
            created = await super().create_run(
                session_id, turn_id, client_request_id, content, model
            )
            deadline = (datetime.now(UTC) + timedelta(milliseconds=150)).isoformat()
            async with self._connect() as connection:
                await connection.execute(
                    "UPDATE runs SET deadline_at = ? WHERE id = ?", (deadline, created.id)
                )
                await connection.commit()
            refreshed = await self.get_run(created.id)
            assert refreshed is not None
            return refreshed

    class HangingExecutor:
        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path
        ) -> tuple[str, bool]:
            await asyncio.Event().wait()
            return "unreachable", False

    settings = make_settings(tmp_path)
    database = NearDeadlineDatabase(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("call-deadline", "read_file", {"path": "note.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(HangingExecutor()),
        )
        created = await service.create_run(session.id, "request-deadline", "Read note")
        try:
            await asyncio.wait_for(service.wait_for_run(created.id), timeout=1)
        finally:
            await service.close()
        return await database.get_run(created.id), await database.list_tool_calls(created.id)

    persisted, calls = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.FAILED
    assert persisted.error_code == "provider_timeout"
    assert calls[0].status is ToolCallStatus.TIMED_OUT


def test_provider_metadata_is_redacted_before_events_and_records(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = RecordingProvider(
        (
            ModelStreamEvent(
                kind="usage", input_tokens=1, provider_response_id="sk-proj-secret123456"
            ),
            ModelStreamEvent(kind="text_delta", text="Done"),
            ModelStreamEvent(
                kind="completed",
                finish_reason="token=private123456",
                provider_response_id="sk-proj-secret123456",
            ),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session()
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database, database, database, database, secret_store, {"openai": provider}
        )
        created = await service.create_run(session.id, "metadata-secret", "Hello")
        await service.wait_for_run(created.id)
        await service.close()
        return await database.list_model_calls(created.id), await database.list_run_events(
            created.id
        )

    calls, events = asyncio.run(run())
    assert calls[0].status.value == "completed"
    assert "private123456" not in str(calls) + str(events)
    assert "sk-proj-secret123456" not in str(calls) + str(events)
    assert b"private123456" not in settings.database_path.read_bytes()
    assert b"sk-proj-secret123456" not in settings.database_path.read_bytes()


def test_secret_looking_tool_call_id_is_rejected_before_persistence(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = RecordingProvider(
        (
            ModelStreamEvent(
                kind="tool_call",
                tool_call=ModelToolCall("sk-proj-secret123456", "list_files", {}),
            ),
            ModelStreamEvent(kind="completed", finish_reason="tool_use"),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(LocalReadToolExecutor()),
        )
        created = await service.create_run(session.id, "secret-tool-id", "List files")
        await service.wait_for_run(created.id)
        await service.close()
        return await database.get_run(created.id), await database.list_tool_calls(created.id)

    run, calls = asyncio.run(run())
    assert run is not None and run.status is RunStatus.FAILED
    assert run.error_code == "provider_invalid_response"
    assert calls == []
    assert b"sk-proj-secret123456" not in settings.database_path.read_bytes()


def test_model_stream_stops_at_tool_call_limit(tmp_path: Path) -> None:
    class TooManyToolsProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__(())
            self.reached_completion = False

        def stream(
            self, request: ModelRequest, api_key: str, user_id: str
        ) -> AsyncGenerator[ModelStreamEvent]:
            async def generate() -> AsyncGenerator[ModelStreamEvent]:
                for index in range(17):
                    yield ModelStreamEvent(
                        kind="tool_call",
                        tool_call=ModelToolCall(f"call-{index}", "list_files", {}),
                    )
                self.reached_completion = True
                yield ModelStreamEvent(kind="completed", finish_reason="tool_use")

            return generate()

    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = TooManyToolsProvider()

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(LocalReadToolExecutor()),
        )
        created = await service.create_run(session.id, "tool-limit", "List files")
        await service.wait_for_run(created.id)
        await service.close()
        return await database.get_run(created.id)

    run = asyncio.run(run())
    assert run is not None and run.error_code == "tool_call_limit"
    assert not provider.reached_completion


def test_model_stream_rejects_oversized_continuation_data(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = RecordingProvider(
        (
            ModelStreamEvent(kind="text_delta", text="Done"),
            ModelStreamEvent(
                kind="continuation_item",
                continuation_item=ModelContinuationItem("openai", "openai:gpt-5.5", "a" * 300_000),
            ),
            ModelStreamEvent(kind="completed", finish_reason="stop"),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session()
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database, database, database, database, secret_store, {"openai": provider}
        )
        created = await service.create_run(session.id, "continuation-limit", "Hello")
        await service.wait_for_run(created.id)
        await service.close()
        return await database.get_run(created.id)

    run = asyncio.run(run())
    assert run is not None and run.status is RunStatus.FAILED
    assert run.error_code == "provider_invalid_response"


def test_cancellation_wins_when_provider_finishes_during_cancel_publication(
    tmp_path: Path,
) -> None:
    class CancelRaceProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__(())
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def stream(
            self, request: ModelRequest, api_key: str, user_id: str
        ) -> AsyncGenerator[ModelStreamEvent]:
            async def generate() -> AsyncGenerator[ModelStreamEvent]:
                self.started.set()
                await self.release.wait()
                yield ModelStreamEvent(kind="text_delta", text="Done")
                yield ModelStreamEvent(kind="completed", finish_reason="stop")

            return generate()

    class BlockingCancelPublisher:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def publish(self, event: RunEvent) -> None:
            if event.event_type is RunEventType.CANCELLATION_REQUESTED:
                self.started.set()
                await self.release.wait()

    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = CancelRaceProvider()
    publisher = BlockingCancelPublisher()

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
            publisher,
        )
        created = await service.create_run(session.id, "cancel-race", "Hello")
        await provider.started.wait()
        cancelling = asyncio.create_task(service.cancel_run(created.id))
        await publisher.started.wait()
        provider.release.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(service.wait_for_run(created.id), return_exceptions=True),
                timeout=1,
            )
        finally:
            publisher.release.set()
            await cancelling
        await service.close()
        return await database.get_run(created.id)

    persisted = asyncio.run(asyncio.wait_for(run(), timeout=2))
    assert persisted is not None and persisted.status is RunStatus.CANCELLED


def test_truncated_tool_result_tells_the_next_model_step(tmp_path: Path) -> None:
    class TruncatingExecutor:
        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path
        ) -> tuple[str, bool]:
            return "partial file content", True

    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("call-1", "read_file", {"path": "a.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="Only part of the file was available."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(TruncatingExecutor()),
        )
        created = await service.create_run(session.id, "truncated-result", "Read a.txt")
        await service.wait_for_run(created.id)
        await service.close()
        return await database.list_run_messages(created.id)

    exchange = asyncio.run(run())
    assert "Output truncated by Trellis" in exchange[1].content
    assert provider.requests[1].messages[-1].content == exchange[1].content


def test_second_model_failure_keeps_the_first_tool_exchange(tmp_path: Path) -> None:
    class FailsOnSecondStep(RecordingProvider):
        def __init__(self) -> None:
            super().__init__(())
            self.calls = 0

        def stream(
            self, request: ModelRequest, api_key: str, user_id: str
        ) -> AsyncGenerator[ModelStreamEvent]:
            self.calls += 1
            step = self.calls

            async def generate() -> AsyncGenerator[ModelStreamEvent]:
                if step == 1:
                    yield ModelStreamEvent(
                        kind="tool_call", tool_call=ModelToolCall("call-1", "list_files", {})
                    )
                    yield ModelStreamEvent(kind="completed", finish_reason="tool_use")
                else:
                    yield ModelStreamEvent(kind="text_delta", text="partial")
                    raise ProviderError("provider_upstream_failed", "Safe provider failure")

            return generate()

    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = FailsOnSecondStep()

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(LocalReadToolExecutor()),
        )
        created = await service.create_run(session.id, "second-step-fails", "List files")
        await service.wait_for_run(created.id)
        await service.close()
        return (
            await database.get_run(created.id),
            await database.list_model_calls(created.id),
            await database.list_run_messages(created.id),
            await database.list_messages(session.id),
        )

    run, calls, exchange, visible = asyncio.run(run())
    assert run is not None and run.status is RunStatus.FAILED
    assert [call.status.value for call in calls] == ["completed", "failed"]
    assert [message.role for message in exchange] == ["assistant", "tool"]
    assert [message.role for message in visible] == ["user"]


def test_run_service_rejects_an_unoffered_tool_call_without_a_final_reply(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = RecordingProvider(
        (
            ModelStreamEvent(kind="text_delta", text="I will read that file."),
            ModelStreamEvent(
                kind="tool_call",
                tool_call=ModelToolCall("call-1", "read_file", {"path": "a.py"}),
            ),
            ModelStreamEvent(kind="completed", finish_reason="tool_use"),
        )
    )

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
        created = await service.create_run(session.id, "request-unoffered", "Read a.py")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_messages(session.id),
            await database.list_model_calls(created.id),
        )
        await service.close()
        return result

    persisted, messages, calls = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.FAILED
    assert persisted.error_code == "tool_execution_unavailable"
    assert [message.role for message in messages] == ["user"]
    assert calls[0].response_snapshot is not None
    assert calls[0].response_snapshot["tool_calls"] == [
        {"id": "call-1", "name": "read_file", "arguments": {"path": "a.py"}}
    ]


def test_provider_failure_keeps_no_partial_assistant_message(tmp_path: Path) -> None:
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
    assert events[-1].event_type is RunEventType.FAILED
    assert all(event.event_type is not RunEventType.ASSISTANT_DELTA for event in events)
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


def test_user_cancellation_transitions_running_run_and_cancels_model_call(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)
    provider = BlockingProvider()

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
        created = await service.create_run(session.id, "request-10", "Stop this")
        await provider.started.wait()
        cancellation = await service.cancel_run(created.id)
        task = service._tasks[created.id]
        repeated_cancellation = await service.cancel_run(created.id)
        cancellation_requests = task.cancelling()
        result = await asyncio.gather(task, return_exceptions=True)
        persisted = await database.get_run(created.id)
        events = await database.list_run_events(created.id)
        calls = await database.list_model_calls(created.id)
        await service.close()
        return (
            cancellation,
            repeated_cancellation,
            cancellation_requests,
            result,
            persisted,
            events,
            calls,
        )

    cancellation, repeated_cancellation, cancellation_requests, result, persisted, events, calls = (
        asyncio.run(run())
    )
    assert cancellation.status is RunStatus.CANCELLING
    assert repeated_cancellation.status is RunStatus.CANCELLING
    assert cancellation_requests == 1
    assert result and isinstance(result[0], asyncio.CancelledError)
    assert persisted is not None and persisted.status is RunStatus.CANCELLED
    assert [event.event_type for event in events][-2:] == [
        RunEventType.CANCELLATION_REQUESTED,
        RunEventType.CANCELLED,
    ]
    assert calls[0].status.value == "cancelled"


def test_user_can_cancel_a_run_waiting_for_a_concurrency_slot(tmp_path: Path) -> None:
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
        first = await service.create_run(first_session.id, "request-11", "Keep running")
        await provider.started.wait()
        second = await service.create_run(second_session.id, "request-12", "Cancel queued")
        cancelled = await service.cancel_run(second.id)
        await asyncio.gather(service._tasks[second.id], return_exceptions=True)
        await service.close()
        return cancelled, await database.get_run(second.id), await database.get_run(first.id)

    cancelled, persisted, first = asyncio.run(run())
    assert cancelled.status is RunStatus.CANCELLED
    assert persisted is not None and persisted.status is RunStatus.CANCELLED
    assert first is not None and first.status is RunStatus.INTERRUPTED


def test_cancelling_orphaned_running_run_reaches_terminal_state(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    secret_store = SecretStore(settings.secrets_path)

    async def run():
        await database.initialize()
        session = await database.create_session()
        model = (await database.list_models())[0]
        created = await database.create_run(
            session.id,
            "orphaned-turn",
            "orphaned-request",
            "Stop after restart",
            model,
        )
        await database.transition_run_record(
            created.id,
            RunStatus.RUNNING,
            RunEventType.STARTED,
            {"status": "running"},
        )
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {},
        )

        cancelled = await service.cancel_run(created.id)
        persisted = await database.get_run(created.id)
        events = await database.list_run_events(created.id)
        await service.close()
        return cancelled, persisted, events

    cancelled, persisted, events = asyncio.run(run())
    assert cancelled.status is RunStatus.CANCELLED
    assert persisted is not None and persisted.status is RunStatus.CANCELLED
    assert [event.event_type for event in events][-2:] == [
        RunEventType.CANCELLATION_REQUESTED,
        RunEventType.CANCELLED,
    ]


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
