"""Terminal runs reconcile calls whose allocation response never reached the runtime."""

import asyncio
from pathlib import Path

import pytest

from app.application.runs import RunService
from app.domain.runtime import ModelCallStatus, RunEventType, RunStatus
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository
from tests.test_runtime_compaction import CompactionProvider


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "interrupted", "completed"])
def test_terminal_transition_closes_only_unfinished_calls_and_preserves_spending(
    tmp_path: Path, terminal: str
) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "account")
        model = (await database.list_models())[0]
        chat = await database.create_session()
        run = await database.create_run(chat.id, "turn", "request", "Hello", model)
        await database.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})
        completed = await database.create_model_call(run.id, 1, model, {})
        completed = await database.update_model_call(
            completed.id,
            ModelCallStatus.COMPLETED,
            input_tokens=101,
            output_tokens=12,
            estimated_cost=0.007,
            response_snapshot={"content": "Paid response"},
        )
        streaming = await database.create_model_call(run.id, 2, model, {})
        streaming = await database.update_model_call(
            streaming.id,
            ModelCallStatus.STREAMING,
            input_tokens=64,
            output_tokens=8,
            estimated_cost=0.003,
            response_snapshot={"usage_context": {"usage_complete": False}},
        )
        pending = await database.create_model_call(run.id, 3, model, {})
        other_chat = await database.create_session()
        other_run = await database.create_run(
            other_chat.id, "other-turn", "other-request", "Other", model
        )
        other_call = await database.create_model_call(other_run.id, 1, model, {})

        if terminal == "completed":
            await database.complete_run(run.id, "Answer")
        else:
            await database.transition_run_record(
                run.id, RunStatus(terminal), RunEventType("run." + terminal), {}
            )

        saved = await database.get_run(run.id)
        assert saved is not None
        calls = await database.list_model_calls(run.id)
        assert calls[0] == completed
        expected = ModelCallStatus.FAILED if terminal == "failed" else ModelCallStatus.CANCELLED
        for call in calls[1:]:
            assert call.status is expected
            assert call.finished_at == saved.finished_at
            assert call.error_code == "run_terminated"
        assert calls[1].input_tokens == streaming.input_tokens == 64
        assert calls[1].output_tokens == streaming.output_tokens == 8
        assert calls[1].estimated_cost == streaming.estimated_cost == 0.003
        assert calls[1].response_snapshot == streaming.response_snapshot
        assert calls[2].id == pending.id and calls[2].estimated_cost is None
        assert (await database.list_model_calls(other_run.id)) == [other_call]

    asyncio.run(exercise())


def test_recovery_closes_allocated_calls_without_losing_reported_usage(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "account")
        chat = await database.create_session()
        model = (await database.list_models())[0]
        run = await database.create_run(chat.id, "turn", "request", "Hello", model)
        await database.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})
        call = await database.create_model_call(run.id, 1, model, {})
        await database.update_model_call(
            call.id,
            ModelCallStatus.STREAMING,
            input_tokens=20,
            output_tokens=3,
            estimated_cost=0.01,
        )

        restarted = MemoryRepository(tmp_path / "account")
        saved = await restarted.recover_expired_run(run.id)
        recovered = (await restarted.list_model_calls(run.id))[0]
        assert saved.status is RunStatus.INTERRUPTED
        assert recovered.status is ModelCallStatus.CANCELLED
        assert recovered.finished_at == saved.finished_at
        assert recovered.error_code == "run_terminated"
        assert (recovered.input_tokens, recovered.output_tokens, recovered.estimated_cost) == (
            20,
            3,
            0.01,
        )

    asyncio.run(exercise())


@pytest.mark.parametrize("stop", ["cancel", "shutdown"])
def test_lost_allocation_response_does_not_leave_an_active_call(tmp_path: Path, stop: str) -> None:
    class LostResponseRepository(MemoryRepository):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.allocated = asyncio.Event()

        async def create_model_call(self, *args, **kwargs):
            await super().create_model_call(*args, **kwargs)
            self.allocated.set()
            await asyncio.Event().wait()

    async def exercise() -> None:
        database = LostResponseRepository(tmp_path / "account")
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = CompactionProvider()
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            chat = await database.create_session()
            run = await service.create_run(chat.id, "turn", "Hello")
            await asyncio.wait_for(database.allocated.wait(), 1)
            if stop == "cancel":
                await service.cancel_run(run.id)
                with pytest.raises(asyncio.CancelledError):
                    await service.wait_for_run(run.id)
            else:
                await service.close()
            saved = await database.get_run(run.id)
            assert saved is not None
            assert saved.status is (
                RunStatus.CANCELLED if stop == "cancel" else RunStatus.INTERRUPTED
            )
            call = (await database.list_model_calls(run.id))[0]
            assert call.status is ModelCallStatus.CANCELLED
            assert call.finished_at == saved.finished_at and call.error_code == "run_terminated"
            assert provider.requests == []
        finally:
            await service.close()

    asyncio.run(exercise())
