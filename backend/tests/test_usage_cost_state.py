import asyncio
from pathlib import Path

from app.domain.runtime import ModelCallStatus, RunEventType, RunStatus
from tests.memory_repository import MemoryRepository


def test_later_unknown_usage_can_clear_an_earlier_cost_estimate(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        model = (await database.list_models())[0]
        run = await database.create_run(session.id, "turn", "request", "Answer", model)
        await database.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})
        call = await database.create_model_call(run.id, 1, model, {})
        call = await database.update_model_call(
            call.id, ModelCallStatus.STREAMING, estimated_cost=0.01
        )
        assert call.estimated_cost == 0.01
        unchanged = await database.update_model_call(call.id, ModelCallStatus.STREAMING)
        assert unchanged.estimated_cost == 0.01
        unknown = await database.update_model_call(
            call.id, ModelCallStatus.STREAMING, clear_estimated_cost=True
        )
        assert unknown.estimated_cost is None
        completed = await database.update_model_call(call.id, ModelCallStatus.COMPLETED)
        assert completed.estimated_cost is None

    asyncio.run(exercise())
