import asyncio
from pathlib import Path

import pytest

from app.application.runs import RunService
from app.domain.runtime import ModelStreamEvent, RunStatus
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository
from tests.test_runtime_service import RecordingProvider


def test_generation_persists_preflight_provenance_and_reserved_output(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = RecordingProvider(
            (
                ModelStreamEvent("text_delta", text="Answer"),
                ModelStreamEvent("completed", finish_kind="final"),
            )
        )
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(session.id, "context", "Current request")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.COMPLETED
            call = (await database.list_model_calls(run.id))[0]
            plan = call.request_snapshot["context"]
            assert isinstance(plan, dict)
            assert plan["count_method"] == "utf8_bytes_estimate_v1"
            assert plan["fits"] is True
            assert plan["input_tokens"] > 0
            assert len(plan["input_hash"]) == 64
            assert plan["sources"][-1]["source_id"] == run.input_message_id
            assert (
                provider.request is not None
                and provider.request.messages[-1].content == "Current request"
            )
        finally:
            await service.close()

    asyncio.run(exercise())


def test_over_budget_current_request_never_dispatches_generation(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = RecordingProvider(
            (
                ModelStreamEvent("text_delta", text="Should not call"),
                ModelStreamEvent("completed", finish_kind="final"),
            )
        )
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(session.id, "context", "x" * 100001)
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.FAILED
            assert saved.error_code == "run_budget_exhausted"
            assert provider.request is None
            assert await database.list_model_calls(run.id) == []
        finally:
            await service.close()

    asyncio.run(exercise())


def test_missing_current_request_stops_before_provider_dispatch(tmp_path: Path) -> None:
    class CappedHistoryRepository(MemoryRepository):
        async def load_context_window(self, session_id: str, *, limit: int = 128):
            from app.domain.context import ContextWindow

            window = await super().load_context_window(session_id, limit=limit)
            return ContextWindow(window.checkpoint, window.messages[:2])

        async def get_turn_messages(self, session_id: str, turn_id: str):
            return []

    async def exercise():
        database = CappedHistoryRepository(tmp_path / "db")
        session = await database.create_session()
        await database.add_user_message(session.id, "old", "Old question")
        await database.add_assistant_message(
            session.id, "old", "Old answer", "openai", "openai:gpt-5.5"
        )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = RecordingProvider(
            (
                ModelStreamEvent("text_delta", text="Incorrect"),
                ModelStreamEvent("completed", finish_kind="final"),
            )
        )
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(session.id, "new", "Answer this request")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.error_code == "context_current_request_missing"
            assert provider.request is None
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("historical", [False, True])
def test_context_event_is_durable_before_generation_and_uses_the_locked_model(
    tmp_path: Path,
    historical: bool,
) -> None:
    class ContextAwareProvider(RecordingProvider):
        def __init__(self, database):
            super().__init__(())
            self.database = database
            self.run_id = None
            self.before_generation = []

        def stream(self, request, api_key, user_id):
            async def generate():
                self.before_generation = await self.database.list_run_events(self.run_id)
                yield ModelStreamEvent("text_delta", text="Answer")
                yield ModelStreamEvent("usage", input_tokens=12, output_tokens=5)
                yield ModelStreamEvent("completed", finish_kind="final")

            return generate()

    class DurablePublisher:
        def __init__(self, database):
            self.database = database
            self.events = []

        async def publish(self, event):
            assert (await self.database.list_run_events(event.run_id))[event.sequence - 1] == event
            self.events.append(event)

    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        if historical:
            for index in range(65):
                await database.add_user_message(session.id, str(index), "Old")
                await database.add_assistant_message(
                    session.id, str(index), "Done", "openai", "openai:gpt-5.5"
                )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = ContextAwareProvider(database)
        publisher = DurablePublisher(database)
        service = RunService(
            database, database, database, database, secrets, {"openai": provider}, publisher
        )
        try:
            run = await service.create_run(session.id, "current", "Current request")
            provider.run_id = run.id
            await database.set_selected_model("anthropic:claude-sonnet-5")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.COMPLETED
            contexts = [
                event for event in publisher.events if event.event_type.value == "run.context"
            ]
            assert len(contexts) == 1
            context = contexts[0]
            assert context in provider.before_generation
            assert context.data["model_id"] == run.model_id == "openai:gpt-5.5"
            assert context.data["status"] == "ready"
            input_tokens = context.data["input_tokens"]
            assert isinstance(input_tokens, int) and input_tokens > 0
            assert context.data["count_method"] == "utf8_bytes_estimate_v1"
            assert context.data["output_reserve_tokens"] == 4096
            checkpoint = await database.load_context_checkpoint(session.id)
            assert (checkpoint is not None) == historical
            assert context.data["checkpoint_id"] == (checkpoint.id if checkpoint else None)
            delta = next(
                event for event in publisher.events if event.event_type.value == "assistant.delta"
            )
            assert context.sequence < delta.sequence
            call = (await database.list_model_calls(run.id))[-1]
            plan = call.request_snapshot["context"]
            assert isinstance(plan, dict)
            assert context.data["input_tokens"] == plan["input_tokens"]
            assert context.data["usable_input_tokens"] == plan["usable_input_tokens"]
        finally:
            await service.close()

    asyncio.run(exercise())
