import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.application.budgets import BudgetPreset
from app.application.errors import ApplicationError, ProviderError
from app.application.runs import RunService
from app.application.tools import ToolExecutionError, ToolRegistry
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
    ToolApprovalDecision,
    ToolCallStatus,
)
from app.infrastructure.local_tools import LocalReadToolExecutor, read_workspace_guidance
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository as Database


def make_settings(data_dir: Path) -> Settings:
    return Settings(environment="test", data_dir=data_dir)


def _database_path(settings: Settings) -> Path:
    return settings.data_dir / "state.db"


def _secrets_path(settings: Settings) -> Path:
    return settings.data_dir / ".env"


class RecordingProvider:
    name: ProviderName = "openai"
    model = "test-model"

    def __init__(
        self, events: tuple[ModelStreamEvent, ...], *, emit_default_usage: bool = True
    ) -> None:
        self.events = events
        self.emit_default_usage = emit_default_usage
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
            saw_usage = False
            for event in self.events:
                if event.kind == "usage":
                    saw_usage = True
                if event.kind == "completed" and not saw_usage and self.emit_default_usage:
                    yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
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
            saw_usage = False
            for event in self.rounds[round_index]:
                if event.kind == "usage":
                    saw_usage = True
                if event.kind == "completed" and not saw_usage and self.emit_default_usage:
                    yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
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
        *,
        budget_preset: BudgetPreset = "conservative",
    ) -> RunSnapshot:
        self.create_started.set()
        await self.allow_create.wait()
        return await super().create_run(
            session_id,
            turn_id,
            client_request_id,
            content,
            model,
            budget_preset=budget_preset,
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    assert model_calls[0].estimated_cost == pytest.approx(0.000046)
    assert events[2].data["estimated_cost_usd"] == pytest.approx(0.000046)
    assert events[2].data["total_tokens"] == 6
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
        "cache_creation_tokens": None,
    }
    assert b"sk-runtime-secret" not in repr(database.state).encode()


def test_run_service_persists_read_tool_exchange_before_a_second_model_call(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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


@pytest.mark.parametrize(
    ("usage", "expected_error"),
    [
        (ModelStreamEvent(kind="usage", input_tokens=100_000, output_tokens=1), "token_limit"),
        (None, "provider_usage_unavailable"),
    ],
)
def test_budget_blocks_tool_execution_after_exhausted_or_missing_usage(
    tmp_path: Path, usage: ModelStreamEvent | None, expected_error: str
) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    (tmp_path / "note.txt").write_text("Read me\n", encoding="utf-8")
    events = [
        ModelStreamEvent(
            kind="tool_call",
            tool_call=ModelToolCall("call-budget", "read_file", {"path": "note.txt"}),
        ),
    ]
    if usage is not None:
        events.append(usage)
    events.append(ModelStreamEvent(kind="completed", finish_reason="tool_use"))
    provider = RecordingProvider(tuple(events), emit_default_usage=usage is not None)

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
        created = await service.create_run(session.id, "request-budget-block", "Read note")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_model_calls(created.id),
            await database.list_tool_calls(created.id),
        )
        await service.close()
        return result

    run, model_calls, tool_calls = asyncio.run(run())
    assert run is not None and run.status is RunStatus.FAILED
    assert run.error_code == expected_error
    assert len(model_calls) == 1
    assert tool_calls == []


def test_estimated_cost_limit_stops_before_publishing_a_final_answer(tmp_path: Path) -> None:
    class TinyCostDatabase(Database):
        async def create_run(
            self,
            session_id: str,
            turn_id: str,
            client_request_id: str,
            content: str,
            model: ModelDescriptor,
            *,
            budget_preset: BudgetPreset = "conservative",
        ) -> RunSnapshot:
            created = await super().create_run(
                session_id,
                turn_id,
                client_request_id,
                content,
                model,
                budget_preset=budget_preset,
            )
            self.state.runs[created.id] = replace(created, max_cost_usd=0.00004)
            refreshed = await self.get_run(created.id)
            assert refreshed is not None
            return refreshed

    settings = make_settings(tmp_path)
    database = TinyCostDatabase(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    provider = RecordingProvider(
        (
            ModelStreamEvent(kind="text_delta", text="Expensive answer"),
            ModelStreamEvent(kind="usage", input_tokens=5, output_tokens=1, cached_tokens=2),
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
        created = await service.create_run(session.id, "request-small-cost", "Answer")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_model_calls(created.id),
            await database.list_messages(session.id),
        )
        await service.close()
        return result

    run, calls, visible = asyncio.run(run())
    assert run is not None and run.error_code == "cost_limit"
    assert calls[0].estimated_cost == pytest.approx(0.000046)
    assert [message.content for message in visible] == ["Answer"]


def test_approved_tool_resumes_after_restart_without_repeating_the_model_call(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    (tmp_path / "note.txt").write_text("safe contents\n", encoding="utf-8")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "note.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="The file is safe."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )
    registry = ToolRegistry(LocalReadToolExecutor(), approval_required_names={"read_file"})

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        first_service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=registry,
        )
        created = await first_service.create_run(session.id, "request-approval", "Read note")
        await first_service.wait_for_run(created.id)
        waiting = await database.get_run(created.id)
        calls = await database.list_tool_calls(created.id)
        assert waiting is not None and waiting.status is RunStatus.WAITING_FOR_APPROVAL
        assert len(provider.requests) == 1
        assert calls[0].status is ToolCallStatus.PENDING
        await database.record_tool_approval_decision(
            created.id, calls[0].id, ToolApprovalDecision.APPROVED
        )
        await first_service.close()

        restarted_database = Database(_database_path(settings))
        second_service = RunService(
            restarted_database,
            restarted_database,
            restarted_database,
            restarted_database,
            secret_store,
            {"openai": provider},
            tool_registry=registry,
        )
        await second_service.resume_decided_approvals()
        await second_service.wait_for_run(created.id)
        result = (
            await restarted_database.get_run(created.id),
            await restarted_database.list_model_calls(created.id),
            await restarted_database.list_tool_calls(created.id),
            await restarted_database.list_run_events(created.id),
            await restarted_database.list_run_messages(created.id),
        )
        await second_service.close()
        return result

    run, model_calls, tool_calls, events, messages = asyncio.run(run())
    assert run is not None and run.status is RunStatus.COMPLETED
    assert [call.step_index for call in model_calls] == [1, 2]
    assert len(provider.requests) == 2
    assert tool_calls[0].status is ToolCallStatus.COMPLETED
    assert [message.role for message in messages] == ["assistant", "tool"]
    assert "safe contents" in messages[1].content
    assert [event.event_type for event in events].count(RunEventType.TOOL_APPROVAL_REQUESTED) == 1
    assert [event.event_type for event in events].count(RunEventType.TOOL_APPROVAL_DECIDED) == 1
    assert [event.event_type for event in events].count(RunEventType.RESUMED) == 1


def test_approved_tool_keeps_execution_budget_after_a_delayed_decision(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    (tmp_path / "note.txt").write_text("safe contents\n", encoding="utf-8")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "note.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="The file is safe."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )
    registry = ToolRegistry(LocalReadToolExecutor(), approval_required_names={"read_file"})

    async def exercise():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        first_service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=registry,
        )
        created = await first_service.create_run(session.id, "request-late-approval", "Read note")
        await first_service.wait_for_run(created.id)
        waiting = await database.get_run(created.id)
        calls = await database.list_tool_calls(created.id)
        assert waiting is not None and waiting.status is RunStatus.WAITING_FOR_APPROVAL

        reference = datetime.now(UTC)
        original_deadline = (reference - timedelta(minutes=2)).isoformat()
        requested_at = (reference - timedelta(minutes=4)).isoformat()
        decided_at = (reference - timedelta(minutes=3)).isoformat()
        database.state.runs[created.id] = replace(waiting, deadline_at=original_deadline)
        database.state.events[created.id] = [
            replace(event, created_at=requested_at)
            if event.event_type is RunEventType.TOOL_APPROVAL_REQUESTED
            else event
            for event in database.state.events[created.id]
        ]
        await database.record_tool_approval_decision(
            created.id, calls[0].id, ToolApprovalDecision.APPROVED
        )
        database.state.tool_calls[calls[0].id] = replace(
            database.state.tool_calls[calls[0].id], approval_decided_at=decided_at
        )
        database.state.events[created.id] = [
            replace(event, created_at=decided_at)
            if event.event_type is RunEventType.TOOL_APPROVAL_DECIDED
            else event
            for event in database.state.events[created.id]
        ]
        await first_service.close()

        restarted_database = Database(_database_path(settings))
        second_service = RunService(
            restarted_database,
            restarted_database,
            restarted_database,
            restarted_database,
            secret_store,
            {"openai": provider},
            tool_registry=registry,
        )
        try:
            await second_service.resume_decided_approvals()
            await second_service.wait_for_run(created.id)
            return (
                await restarted_database.get_run(created.id),
                await restarted_database.list_tool_calls(created.id),
                await restarted_database.list_run_events(created.id),
                reference,
            )
        finally:
            await second_service.close()

    run, calls, events, reference = asyncio.run(exercise())
    assert run is not None and run.status is RunStatus.COMPLETED
    assert calls[0].status is ToolCallStatus.COMPLETED
    assert len(provider.requests) == 2
    resumed = next(event for event in events if event.event_type is RunEventType.RESUMED)
    assert resumed.data["deadline_at"] == run.deadline_at
    assert datetime.fromisoformat(run.deadline_at.replace("Z", "+00:00")) > (
        reference + timedelta(seconds=110)
    )
    assert datetime.fromisoformat(run.deadline_at.replace("Z", "+00:00")) < (
        reference + timedelta(seconds=130)
    )


def test_cancelling_while_waiting_for_approval_closes_the_tool_call(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "note.txt"}),
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
            tool_registry=ToolRegistry(
                LocalReadToolExecutor(), approval_required_names={"read_file"}
            ),
        )
        created = await service.create_run(session.id, "request-cancel-approval", "Read note")
        await service.wait_for_run(created.id)
        waiting = await database.get_run(created.id)
        assert waiting is not None and waiting.status is RunStatus.WAITING_FOR_APPROVAL
        cancelled = await service.cancel_run(created.id)
        calls = await database.list_tool_calls(created.id)
        events = await database.list_run_events(created.id)
        await service.close()
        return cancelled, calls, events

    cancelled, calls, events = asyncio.run(run())
    assert cancelled.status is RunStatus.CANCELLED
    assert calls[0].status is ToolCallStatus.CANCELLED
    assert events[-2].event_type is RunEventType.TOOL_RESULT
    assert events[-1].event_type is RunEventType.CANCELLED


def test_cancelling_an_approved_run_before_it_gets_a_slot_finishes_cancellation(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "note.txt"}),
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
            tool_registry=ToolRegistry(
                LocalReadToolExecutor(), approval_required_names={"read_file"}
            ),
            max_concurrent_runs=1,
        )
        created = await service.create_run(session.id, "request-slot-cancel", "Read note")
        await service.wait_for_run(created.id)
        calls = await database.list_tool_calls(created.id)
        await service._slots.acquire()
        await service.respond_to_tool_approval(
            created.id, calls[0].id, ToolApprovalDecision.APPROVED
        )
        pending_task = service._tasks[created.id]
        await service.cancel_run(created.id)
        await asyncio.gather(pending_task, return_exceptions=True)
        await asyncio.gather(service.wait_for_run(created.id), return_exceptions=True)
        service._slots.release()
        result = await database.get_run(created.id), await database.list_tool_calls(created.id)
        await service.close()
        return result

    persisted, calls = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.CANCELLED
    assert calls[0].status is ToolCallStatus.CANCELLED


def test_shutdown_during_approval_notification_preserves_waiting_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "note.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
        )
    )

    class BlockingApprovalPublisher:
        def __init__(self) -> None:
            self.request_published = asyncio.Event()

        async def publish(self, event: RunEvent) -> None:
            if event.event_type is RunEventType.TOOL_APPROVAL_REQUESTED:
                self.request_published.set()
                await asyncio.Event().wait()

    async def run():
        await database.initialize()
        session = await database.create_session(str(tmp_path))
        await secret_store.set("openai", "sk-runtime-secret")
        publisher = BlockingApprovalPublisher()
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            publisher,
            tool_registry=ToolRegistry(
                LocalReadToolExecutor(), approval_required_names={"read_file"}
            ),
        )
        created = await service.create_run(session.id, "request-shutdown-approval", "Read note")
        await publisher.request_published.wait()
        await service.close()
        return await database.get_run(created.id), await database.list_tool_calls(created.id)

    persisted, calls = asyncio.run(run())
    assert persisted is not None and persisted.status is RunStatus.WAITING_FOR_APPROVAL
    assert calls[0].status is ToolCallStatus.PENDING
    assert "Unexpected failure" not in caplog.text


def test_denial_becomes_a_tool_result_and_late_decisions_are_rejected(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "note.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="I could not read the note."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    class CountingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path
        ) -> tuple[str, bool]:
            self.calls += 1
            return "unexpected", False

    executor = CountingExecutor()

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
            tool_registry=ToolRegistry(executor, approval_required_names={"read_file"}),
        )
        created = await service.create_run(session.id, "request-deny", "Read note")
        await service.wait_for_run(created.id)
        call = (await database.list_tool_calls(created.id))[0]
        await service.respond_to_tool_approval(created.id, call.id, ToolApprovalDecision.DENIED)
        await service.wait_for_run(created.id)
        completed = await database.get_run(created.id)
        with pytest.raises(ApplicationError) as duplicate:
            await service.respond_to_tool_approval(created.id, call.id, ToolApprovalDecision.DENIED)
        with pytest.raises(ApplicationError) as conflicting:
            await service.respond_to_tool_approval(
                created.id, call.id, ToolApprovalDecision.APPROVED
            )
        result = (
            completed,
            await database.list_tool_calls(created.id),
            await database.list_run_messages(created.id),
            await database.list_run_events(created.id),
            duplicate.value.code,
            conflicting.value.code,
        )
        await service.close()
        return result

    run, calls, exchange, events, duplicate_code, conflict_code = asyncio.run(run())
    assert run is not None and run.status is RunStatus.COMPLETED
    assert calls[0].status is ToolCallStatus.DENIED
    assert executor.calls == 0
    assert "approval_denied" in exchange[1].content
    assert "approval_denied" in provider.requests[1].messages[-1].content
    assert [event.event_type for event in events].count(RunEventType.TOOL_APPROVAL_DECIDED) == 1
    assert duplicate_code == conflict_code == "approval_not_pending"


def test_preview_failure_is_a_tool_result_without_requesting_approval(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "missing.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="The file could not be read."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    class FailingPreviewRegistry(ToolRegistry):
        async def approval_preview(
            self, call: ModelToolCall, workspace_root: Path
        ) -> dict[str, object] | None:
            raise ToolExecutionError("path_not_found", "The file does not exist.")

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
            tool_registry=FailingPreviewRegistry(
                LocalReadToolExecutor(), approval_required_names={"read_file"}
            ),
        )
        created = await service.create_run(session.id, "request-preview-failure", "Read note")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_tool_calls(created.id),
            await database.list_run_messages(created.id),
            await database.list_run_events(created.id),
        )
        await service.close()
        return result

    run, calls, exchange, events = asyncio.run(run())
    assert run is not None and run.status is RunStatus.COMPLETED
    assert calls[0].status is ToolCallStatus.FAILED
    assert "path_not_found" in exchange[1].content
    assert RunEventType.TOOL_APPROVAL_REQUESTED not in [event.event_type for event in events]
    assert len(provider.requests) == 2


def test_secret_in_approval_preview_is_not_persisted_or_shown(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "note.txt"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="I cannot show that preview."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    class SecretPreviewRegistry(ToolRegistry):
        async def approval_preview(
            self, call: ModelToolCall, workspace_root: Path
        ) -> dict[str, object] | None:
            return {"diff": "OPENAI_API_KEY=sk-previewsecret123"}

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
            tool_registry=SecretPreviewRegistry(
                LocalReadToolExecutor(), approval_required_names={"read_file"}
            ),
        )
        created = await service.create_run(session.id, "request-secret-preview", "Read note")
        await service.wait_for_run(created.id)
        result = (
            await database.get_run(created.id),
            await database.list_run_messages(created.id),
            await database.list_run_events(created.id),
        )
        await service.close()
        return result

    run, exchange, events = asyncio.run(run())
    assert run is not None and run.status is RunStatus.COMPLETED
    assert "sensitive_approval_preview" in exchange[1].content
    assert RunEventType.TOOL_APPROVAL_REQUESTED not in [event.event_type for event in events]
    assert b"sk-previewsecret123" not in repr(database.state).encode()


def test_tool_only_response_records_two_results_before_continuing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    assert b"sk-secret123" not in repr(database.state).encode()


def test_streamed_secret_split_across_chunks_is_redacted_before_persistence(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    assert b"secret123456" not in repr(database.state).encode()
    assert "[REDACTED]" in visible[-1].content


def test_cancelling_during_a_tool_records_a_terminal_result(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
            *,
            budget_preset: BudgetPreset = "conservative",
        ) -> RunSnapshot:
            created = await super().create_run(
                session_id,
                turn_id,
                client_request_id,
                content,
                model,
                budget_preset=budget_preset,
            )
            deadline = (datetime.now(UTC) + timedelta(milliseconds=150)).isoformat()
            self.state.runs[created.id] = replace(created, deadline_at=deadline)
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
    database = NearDeadlineDatabase(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    assert b"private123456" not in repr(database.state).encode()
    assert b"sk-proj-secret123456" not in repr(database.state).encode()


def test_secret_looking_tool_call_id_is_rejected_before_persistence(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    assert b"sk-proj-secret123456" not in repr(database.state).encode()


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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
                    yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
                    yield ModelStreamEvent(kind="completed", finish_reason="tool_use")
                else:
                    yield ModelStreamEvent(kind="text_delta", text="partial")
                    raise ProviderError("provider_upstream_failed", "Safe provider failure")

            return generate()

    settings = make_settings(tmp_path)
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(make_settings(tmp_path)))
    secret_store = SecretStore(_secrets_path(make_settings(tmp_path)))

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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))

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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    assert repeated_cancellation.status in {RunStatus.CANCELLING, RunStatus.CANCELLED}
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))

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
    database = Database(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = OneShotGetFailureDatabase(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
    database = SlowCreateRunDatabase(_database_path(settings))
    secret_store = SecretStore(_secrets_path(settings))
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
