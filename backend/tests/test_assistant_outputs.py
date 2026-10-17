import asyncio
from pathlib import Path

import pytest

from app.domain.runtime import ModelCallStatus, RunEventType, RunStatus
from tests.memory_repository import MemoryRepository


async def running_call(database: MemoryRepository, turn: str = "turn") -> tuple[str, str, str]:
    session = await database.create_session()
    model = (await database.list_models())[0]
    run = await database.create_run(session.id, turn, turn, "Hello", model)
    await database.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})
    call = await database.create_model_call(run.id, 1, model, {})
    await database.update_model_call(call.id, ModelCallStatus.STREAMING)
    return session.id, run.id, call.id


def test_published_batches_are_durable_and_idempotent(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path)
        session_id, run_id, call_id = await running_call(database)
        first, event = await database.append_assistant_batch(run_id, call_id, 0, " Hello")
        duplicate, duplicate_event = await database.append_assistant_batch(
            run_id, call_id, 0, " Hello"
        )
        assert duplicate == first and duplicate_event == event
        second, _ = await database.append_assistant_batch(run_id, call_id, 1, "\nworld ")
        restarted = MemoryRepository(tmp_path)
        assert (await restarted.list_assistant_outputs(session_id))[0].content == " Hello\nworld "
        assert second.id == first.id and second.next_batch_index == 2
        assert [message.role for message in await database.list_messages(session_id)] == ["user"]
        assert [
            item.data["text"]
            for item in await database.list_run_events(run_id)
            if item.event_type is RunEventType.ASSISTANT_DELTA
        ] == [" Hello", "\nworld "]

    asyncio.run(exercise())


def test_conflicting_or_out_of_order_batches_do_not_change_saved_text(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path)
        session_id, run_id, call_id = await running_call(database)
        await database.append_assistant_batch(run_id, call_id, 0, "first")
        with pytest.raises(ValueError, match="conflict"):
            await database.append_assistant_batch(run_id, call_id, 0, "different")
        with pytest.raises(ValueError, match="order"):
            await database.append_assistant_batch(run_id, call_id, 2, "skip")
        assert (await database.list_assistant_outputs(session_id))[0].content == "first"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "status,event_type",
    [
        (RunStatus.FAILED, RunEventType.FAILED),
        (RunStatus.INTERRUPTED, RunEventType.INTERRUPTED),
    ],
)
def test_terminal_runs_preserve_partial_outputs(tmp_path: Path, status, event_type) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path)
        session_id, run_id, call_id = await running_call(database)
        await database.append_assistant_batch(run_id, call_id, 0, "unfinished")
        await database.transition_run_record(
            run_id, status, event_type, {}, stop_reason="provider_failure"
        )
        output = (await MemoryRepository(tmp_path).list_assistant_outputs(session_id))[0]
        assert output.status == "partial" and output.stop_reason == "provider_failure"
        assert output.content == "unfinished" and output.promoted_message_id is None
        with pytest.raises(ValueError, match="running"):
            await database.append_assistant_batch(run_id, call_id, 1, "too late")

    asyncio.run(exercise())


def test_completion_promotes_exact_published_text(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path)
        session_id, run_id, call_id = await running_call(database)
        await database.append_assistant_batch(run_id, call_id, 0, " answer\n")
        with pytest.raises(ValueError, match="published"):
            await database.complete_run(run_id, "answer")
        await database.update_model_call(call_id, ModelCallStatus.COMPLETED)
        message, _ = await database.complete_run(run_id, " answer\n")
        assert message.content == " answer\n"
        output = (await database.list_assistant_outputs(session_id))[0]
        assert output.status == "complete" and output.promoted_message_id == message.id
        assert output.content == message.content

    asyncio.run(exercise())


def test_batches_reject_calls_from_another_run_and_oversized_text(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path)
        _, run_id, call_id = await running_call(database)
        _, other_run_id, _ = await running_call(database, "other")
        with pytest.raises(ValueError, match="model call"):
            await database.append_assistant_batch(other_run_id, call_id, 0, "wrong")
        with pytest.raises(ValueError, match="batch"):
            await database.append_assistant_batch(run_id, call_id, 0, "x" * 4097)

    asyncio.run(exercise())


def test_session_details_restore_partial_outputs(tmp_path: Path) -> None:
    from app.application.sessions import SessionService

    async def exercise():
        database = MemoryRepository(tmp_path)
        session_id, run_id, call_id = await running_call(database)
        await database.append_assistant_batch(run_id, call_id, 0, "saved partial")
        await database.transition_run_record(run_id, RunStatus.FAILED, RunEventType.FAILED, {})
        detail = await SessionService(MemoryRepository(tmp_path)).get(session_id)
        assert detail is not None
        assert detail.assistant_outputs[0].content == "saved partial"
        assert detail.assistant_outputs[0].status == "partial"
        assert [message.role for message in detail.messages] == ["user"]

    asyncio.run(exercise())


def test_supabase_output_adapter_preserves_payload_and_read_ownership() -> None:
    import json
    from uuid import UUID

    import httpx

    from app.core.config import Settings
    from app.infrastructure.supabase_repository import SupabaseRepository
    from tests.support import verified_token_source
    from tests.test_supabase_repository import _model, _run_row

    row = _run_row()
    owner = UUID(str(row["user_id"]))
    output_row = {
        "id": "00000000-0000-0000-0000-000000000201",
        "user_id": str(owner),
        "chat_id": row["chat_id"],
        "turn_id": row["turn_id"],
        "run_id": row["id"],
        "model_call_id": "00000000-0000-0000-0000-000000000202",
        "step_index": 1,
        "content": " preserved\n",
        "status": "streaming",
        "next_batch_index": 1,
        "stop_reason": None,
        "promoted_message_id": None,
        "created_at": row["created_at"],
    }
    payloads = []

    def handle(request):
        if request.method == "GET":
            assert request.url.params["chat_id"] == "eq." + str(row["chat_id"])
            return httpx.Response(200, json=[output_row])
        body = json.loads(request.content)
        payloads.append(body)
        if body["p_action"] == "create_run":
            return httpx.Response(200, json={"run": row, "lease_acquired": True})
        if body["p_action"] == "finish_assistant_output":
            return httpx.Response(
                200, json={**output_row, "status": "complete", "stop_reason": "tool_continuation"}
            )
        return httpx.Response(
            200,
            json={
                "output": output_row,
                "event": {
                    "run_id": row["id"],
                    "user_id": str(owner),
                    "sequence": 2,
                    "event_type": "assistant.delta",
                    "event_version": 1,
                    "data": {"text": " preserved\n"},
                    "created_at": row["created_at"],
                },
            },
        )

    async def exercise():
        settings = Settings(
            _env_file=None,
            supabase_url="http://localhost:54321",
            supabase_publishable_key="sb_publishable_test-key",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            repository = SupabaseRepository(settings, owner, verified_token_source(owner), client)
            run = await repository.create_run(
                str(row["chat_id"]), "turn", "request", "Hi", _model()
            )
            output, event = await repository.append_assistant_batch(
                run.id, str(output_row["model_call_id"]), 0, " preserved\n"
            )
            assert output.content == event.data["text"] == " preserved\n"
            saved = await repository.list_assistant_outputs(str(row["chat_id"]))
            assert saved == [output]
            finished = await repository.finish_assistant_output(run.id, output.model_call_id)
            assert finished is not None and finished.status == "complete"
            assert payloads[1]["p_payload"]["text"] == " preserved\n"
            assert payloads[1]["p_payload"]["batch_index"] == 0
            assert len(payloads[1]["p_capability"]) >= 32
            output_row["user_id"] = "00000000-0000-0000-0000-000000000999"
            with pytest.raises(RuntimeError):
                await repository.list_assistant_outputs(str(row["chat_id"]))

    asyncio.run(exercise())
