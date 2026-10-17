import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.domain.runtime import ModelRequest, ModelStreamEvent, RunEventType, RunStatus
from app.infrastructure.secrets import SecretStore
from app.infrastructure.supabase_repository import SupabaseRepository
from tests.memory_repository import MemoryRepository
from tests.support import verified_token_source
from tests.test_supabase_repository import ALICE, SETTINGS


class UsageFlood:
    name = "openai"
    model = "test-model"
    closed = False

    async def complete(self, *_args: object, **_kwargs: object) -> str:
        raise NotImplementedError

    def stream(
        self, request: ModelRequest, api_key: str, user_id: str
    ) -> AsyncGenerator[ModelStreamEvent]:
        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            try:
                yield ModelStreamEvent(kind="text_delta", text="partial answer " * 400)
                for _ in range(9000):
                    yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
                yield ModelStreamEvent(kind="completed")
            finally:
                self.closed = True

        return generate()


def test_provider_event_flood_stops_with_partial_output_and_replayable_terminal_event(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "events")
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = UsageFlood()
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            chat = await database.create_session()
            run = await service.create_run(chat.id, "flood", "Hello")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.FAILED
            assert saved.error_code == "event_storage_limit"
            assert saved.last_event_sequence <= 8192 and provider.closed
            outputs = await database.list_assistant_outputs(chat.id)
            assert len(outputs) == 1 and outputs[0].status == "partial"
            assert outputs[0].content.startswith("partial answer ")
            events = []
            cursor = 0
            while page := await database.list_run_events(run.id, cursor):
                events.extend(page)
                cursor = page[-1].sequence
            assert len(events) == saved.last_event_sequence
            assert events[-1].event_type is RunEventType.FAILED
            assert [event.sequence for event in events] == list(range(1, len(events) + 1))
            assert await database.list_run_events(run.id, events[-2].sequence) == events[-1:]
        finally:
            await service.close()

    asyncio.run(exercise())


def test_oversized_event_does_not_change_saved_history_or_consume_terminal_reserve(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "events")
        chat = await database.create_session()
        model = (await database.list_models())[0]
        run = await database.create_run(chat.id, "large", "large", "Hello", model)
        before = await database.list_run_events(run.id)
        with pytest.raises(ApplicationError) as error:
            await database.append_run_event(
                run.id, RunEventType.MODEL_USAGE, {"value": "😀" * 70000}
            )
        assert error.value.code == "event_storage_limit"
        assert await database.list_run_events(run.id) == before
        terminal = await database.transition_run_record(
            run.id, RunStatus.FAILED, RunEventType.FAILED, {"code": "event_storage_limit"}
        )
        assert terminal.sequence == len(before) + 1

    asyncio.run(exercise())


def test_cloud_event_storage_error_is_safe_application_error() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    400,
                    json={
                        "code": "22023",
                        "message": "event_storage_limit",
                        "details": "private payload",
                    },
                )
            )
        ) as client:
            database = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            database._capabilities["run"] = "capability-with-at-least-thirty-two-characters"
            database._lease_expires["run"] = datetime.now(UTC) + timedelta(seconds=60)
            with pytest.raises(ApplicationError) as error:
                await database.append_run_event(
                    "run", RunEventType.MODEL_USAGE, {"input_tokens": 1}
                )
            assert error.value.code == "event_storage_limit"
            assert "private" not in error.value.message

    asyncio.run(exercise())


def test_failed_bulk_tool_message_rolls_back_saved_history_and_calls(tmp_path: Path) -> None:
    from app.domain.runtime import ModelCallStatus, ModelMessage, ModelToolCall

    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "atomic")
        chat = await database.create_session()
        model = (await database.list_models())[0]
        run = await database.create_run(chat.id, "atomic", "atomic", "Hello", model)
        await database.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})
        call = await database.create_model_call(run.id, 1, model, {})
        await database.update_model_call(call.id, ModelCallStatus.STREAMING)
        await database.update_model_call(call.id, ModelCallStatus.COMPLETED)
        before = await database.list_run_events(run.id)
        with pytest.raises(ApplicationError) as error:
            await database.record_assistant_message(
                run.id,
                call.id,
                ModelMessage(
                    role="assistant",
                    content="partial",
                    tool_calls=(ModelToolCall("huge", "lookup_record", {"record": "x" * 262144}),),
                ),
            )
        assert error.value.code == "event_storage_limit"
        assert await database.list_run_events(run.id) == before
        assert await database.list_tool_calls(run.id) == []
        assert await database.list_run_messages(run.id) == []

    asyncio.run(exercise())


def test_event_limit_during_tool_cleanup_still_persists_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import BaseModel

    from app.application.tools import ToolRegistration, ToolRegistry
    from app.domain import event_limits
    from app.domain.runtime import (
        ModelMessage,
        ModelToolCall,
        RunEvent,
        RunMessageRecord,
        ToolCallRecord,
        ToolCallStatus,
    )
    from tests.test_runtime_service import SequencedProvider

    monkeypatch.setattr(event_limits, "MAX_RUN_EVENTS", 80)

    class LookupArguments(BaseModel):
        record: str

    class LookupExecutor:
        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path | None
        ) -> tuple[str, bool]:
            raise AssertionError("storage exhaustion must stop tool execution")

    class FilledRepository(MemoryRepository):
        async def record_assistant_message(
            self, run_id: str, model_call_id: str, message: ModelMessage
        ) -> tuple[RunMessageRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]:
            result = await super().record_assistant_message(run_id, model_call_id, message)
            while len(self.state.events[run_id]) < 64:
                await self.append_run_event(run_id, RunEventType.MODEL_USAGE, {})
            return result

    async def exercise() -> None:
        database = FilledRepository(tmp_path / "pending")
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = SequencedProvider(
            (
                (
                    *(
                        ModelStreamEvent(
                            kind="tool_call",
                            tool_call=ModelToolCall(
                                str(index), "lookup_record", {"record": "entry"}
                            ),
                        )
                        for index in range(30)
                    ),
                    ModelStreamEvent(kind="completed"),
                ),
            )
        )
        registry = ToolRegistry(
            additional_tools=(
                ToolRegistration(
                    "lookup_record", "Look up record", LookupArguments, LookupExecutor()
                ),
            )
        )
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            tool_registry=registry,
        )
        try:
            chat = await database.create_session()
            run = await service.create_run(chat.id, "pending", "Hello", budget_preset="longer")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.FAILED
            assert saved.error_code == "event_storage_limit" and saved.last_event_sequence <= 80
            calls = await database.list_tool_calls(run.id)
            assert len(calls) == 30 and all(call.status is ToolCallStatus.FAILED for call in calls)
        finally:
            await service.close()

    asyncio.run(exercise())


def test_runtime_rejects_one_oversized_tool_event_without_partial_tool_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import BaseModel

    from app.application.tools import ToolRegistration, ToolRegistry
    from app.domain import event_limits
    from app.domain.runtime import ModelToolCall
    from tests.test_runtime_service import RecordingProvider

    monkeypatch.setattr(event_limits, "MAX_SINGLE_EVENT_BYTES", 1024)

    class LookupArguments(BaseModel):
        record: str

    class LookupExecutor:
        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path | None
        ) -> tuple[str, bool]:
            raise AssertionError("oversized tool request must not execute")

    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "huge")
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = RecordingProvider(
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("huge", "lookup_record", {"record": "x" * 2048}),
                ),
                ModelStreamEvent(kind="completed"),
            )
        )
        registry = ToolRegistry(
            additional_tools=(
                ToolRegistration(
                    "lookup_record", "Look up record", LookupArguments, LookupExecutor()
                ),
            )
        )
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            tool_registry=registry,
        )
        try:
            chat = await database.create_session()
            run = await service.create_run(chat.id, "huge", "Hello")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.FAILED
            assert saved.error_code == "event_storage_limit", saved.error_message
            assert await database.list_tool_calls(run.id) == []
            assert await database.list_run_messages(run.id) == []
            events = await database.list_run_events(run.id)
            assert events[-1].event_type is RunEventType.FAILED
        finally:
            await service.close()

    asyncio.run(exercise())
