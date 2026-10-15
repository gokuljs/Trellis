import asyncio
from pathlib import Path

import pytest

from app.application.errors import ProviderError
from app.application.runs import RunService
from app.domain.runtime import ModelStreamEvent, RunStatus
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository
from tests.test_runtime_service import RecordingProvider


class CompactionProvider(RecordingProvider):
    def __init__(self, *, fail_summary: bool = False):
        super().__init__(())
        self.requests = []
        self.fail_summary = fail_summary

    def stream(self, request, api_key, user_id):
        self.requests.append(request)

        async def generate():
            summary = request.system_instructions.startswith("Summarize the historical")
            yield ModelStreamEvent("text_delta", text="Remember tea." if summary else "Answer")
            yield ModelStreamEvent("usage", input_tokens=12, output_tokens=5)
            if summary and self.fail_summary:
                raise ProviderError("provider_upstream_failed", "Safe failure")
            yield ModelStreamEvent("completed", finish_kind="final")

        return generate()


@pytest.mark.parametrize("turns", [20, 65, 190])
def test_runtime_compacts_history_and_keeps_current_request_and_private_summary(
    tmp_path: Path, turns: int
) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        for index in range(turns):
            await database.add_user_message(
                session.id, str(index), "old " * (1600 if turns == 20 else 1)
            )
            await database.add_assistant_message(
                session.id, str(index), "Done", "openai", "openai:gpt-5.5"
            )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = CompactionProvider()
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(session.id, "current", "Current request")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.COMPLETED
            checkpoint = await database.load_context_checkpoint(session.id)
            assert checkpoint is not None and checkpoint.draft.summary == "Remember tea."
            calls = await database.list_model_calls(run.id)
            purposes = [call.request_snapshot.get("purpose", "generation") for call in calls]
            assert purposes[-1] == "generation"
            assert purposes[:-1] and set(purposes[:-1]) == {"summary"}
            if turns == 190:
                assert len(calls) > 2
            assert calls[0].input_tokens == 12 and calls[0].output_tokens == 5
            assert provider.requests[-1].messages[-1].content == "Current request"
            assert provider.requests[-1].messages[0].content.endswith("Remember tea.")
            assert "old " not in provider.requests[-1].messages[0].content
            outputs = await database.list_assistant_outputs(session.id)
            assert [output.content for output in outputs] == ["Answer"]
            assert [message.content for message in await database.list_messages(session.id)][
                -2:
            ] == ["Current request", "Answer"]
        finally:
            await service.close()

    asyncio.run(exercise())


def test_failed_summary_keeps_billed_usage_and_leaves_checkpoint_uninstalled(
    tmp_path: Path,
) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        for index in range(20):
            await database.add_user_message(session.id, str(index), "old " * 1600)
            await database.add_assistant_message(
                session.id, str(index), "Done", "openai", "openai:gpt-5.5"
            )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = CompactionProvider(fail_summary=True)
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(session.id, "current", "Current request")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.FAILED
            assert saved.error_code == "provider_upstream_failed"
            calls = await database.list_model_calls(run.id)
            assert len(calls) == 1 and calls[0].status.value == "failed"
            assert calls[0].input_tokens == 12 and calls[0].estimated_cost is not None
            assert await database.load_context_checkpoint(session.id) is None
            assert await database.list_assistant_outputs(session.id) == []
        finally:
            await service.close()

    asyncio.run(exercise())


def test_cancelling_summary_preserves_reported_usage_without_checkpoint_or_answer(
    tmp_path: Path,
) -> None:
    class PausedProvider(CompactionProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        def stream(self, request, api_key, user_id):
            async def generate():
                yield ModelStreamEvent("usage", input_tokens=12, output_tokens=5)
                self.started.set()
                await asyncio.Event().wait()

            return generate()

    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        for index in range(65):
            await database.add_user_message(session.id, str(index), "Old")
            await database.add_assistant_message(
                session.id, str(index), "Done", "openai", "openai:gpt-5.5"
            )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = PausedProvider()
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(session.id, "current", "Current request")
            await asyncio.wait_for(provider.started.wait(), 1)
            await service.cancel_run(run.id)
            with pytest.raises(asyncio.CancelledError):
                await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.CANCELLED
            call = (await database.list_model_calls(run.id))[0]
            assert call.status.value == "cancelled" and call.input_tokens == 12
            assert await database.load_context_checkpoint(session.id) is None
            assert await database.list_assistant_outputs(session.id) == []
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("summarizing", [False, True])
def test_changed_cache_write_total_invalidates_previously_known_ttl(
    tmp_path: Path, summarizing: bool
) -> None:
    class ChangingUsageProvider(CompactionProvider):
        def stream(self, request, api_key, user_id):
            self.requests.append(request)

            async def generate():
                yield ModelStreamEvent("text_delta", text="Answer")
                yield ModelStreamEvent(
                    "usage",
                    input_tokens=12,
                    output_tokens=5,
                    cached_tokens=0,
                    cache_creation_tokens=10,
                    cache_creation_1h_tokens=10,
                )
                yield ModelStreamEvent(
                    "usage",
                    input_tokens=12,
                    output_tokens=5,
                    cached_tokens=0,
                    cache_creation_tokens=20,
                    cache_creation_1h_tokens=None,
                )
                yield ModelStreamEvent("completed", finish_kind="final")

            return generate()

    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        await database.set_selected_model("anthropic:claude-sonnet-5")
        session = await database.create_session()
        if summarizing:
            for index in range(65):
                await database.add_user_message(session.id, str(index), "Old")
                await database.add_assistant_message(
                    session.id, str(index), "Done", "anthropic", "anthropic:claude-sonnet-5"
                )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("anthropic", "test-key")
        provider = ChangingUsageProvider()
        service = RunService(
            database, database, database, database, secrets, {"anthropic": provider}
        )
        try:
            run = await service.create_run(session.id, "current", "Current request")
            await service.wait_for_run(run.id)
            calls = await database.list_model_calls(run.id)
            assert len(calls) == 1
            call = calls[0]
            assert call.cache_creation_tokens == 20
            assert call.response_snapshot is not None
            billing = call.response_snapshot["usage_context"]
            assert isinstance(billing, dict)
            assert billing["cache_creation_1h_tokens"] is None
            assert call.estimated_cost is None
            assert service._budget_totals(run.model_id, calls) == (37, None)
            assert len(provider.requests) == 1
            saved = await database.get_run(run.id)
            assert saved is not None
            if summarizing:
                assert saved.status is RunStatus.FAILED
                assert saved.error_code == "provider_usage_unavailable"
                assert await database.load_context_checkpoint(session.id) is None
            else:
                assert saved.status is RunStatus.COMPLETED
        finally:
            await service.close()

    asyncio.run(exercise())
