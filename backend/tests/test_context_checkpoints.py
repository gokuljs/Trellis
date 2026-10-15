import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.application.errors import ApplicationError
from app.domain.context import CheckpointSource, ContextCheckpointDraft
from app.domain.runtime import ModelCallStatus, RunEventType, RunStatus
from app.infrastructure.supabase_repository import SupabaseRepository
from tests.memory_repository import MemoryRepository
from tests.support import verified_token_source
from tests.test_supabase_repository import ALICE, BOB, SETTINGS, _model, _run_row


def draft(call_id: str, sources: tuple[CheckpointSource, ...]) -> ContextCheckpointDraft:
    return ContextCheckpointDraft(
        str(uuid4()),
        call_id,
        sources,
        "Remember the earlier request.",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        1000,
        100,
    )


async def fixture(database: MemoryRepository) -> tuple[str, str, ContextCheckpointDraft]:
    session = await database.create_session()
    user = await database.add_user_message(session.id, "old", "Remember my preference.")
    assistant = await database.add_assistant_message(
        session.id, "old", "I will remember.", "openai", "openai:gpt-5.5"
    )
    model = (await database.list_models())[0]
    run = await database.create_run(session.id, "now", "now", "Continue", model)
    await database.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})
    call = await database.create_model_call(run.id, 1, model, {"purpose": "summary"})
    await database.update_model_call(call.id, ModelCallStatus.COMPLETED)
    return (
        session.id,
        run.id,
        draft(
            call.id,
            (
                CheckpointSource(user.id, "message", "old"),
                CheckpointSource(assistant.id, "message", "old"),
            ),
        ),
    )


def test_checkpoint_save_is_durable_idempotent_and_does_not_delete_messages(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        session_id, run_id, candidate = await fixture(database)
        messages_before = await database.list_messages(session_id)
        saved = await database.save_context_checkpoint(run_id, 0, candidate)
        assert saved is not None and saved.revision == 1 and saved.covered_message_ordinal == 2
        assert saved.parent_id is None
        assert await database.save_context_checkpoint(run_id, 0, candidate) == saved
        restarted = MemoryRepository(tmp_path)
        assert await restarted.load_context_checkpoint(session_id) == saved
        assert await restarted.list_messages(session_id) == messages_before
        assert [
            message.content for message in await restarted.list_context_messages(session_id, 0)
        ] == ["Continue"]

    asyncio.run(exercise())


def test_stale_revision_and_conflicting_retry_keep_the_installed_checkpoint(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        session_id, run_id, candidate = await fixture(database)
        saved = await database.save_context_checkpoint(run_id, 0, candidate)
        with pytest.raises(ApplicationError) as conflict:
            await database.save_context_checkpoint(run_id, 0, replace(candidate, id=str(uuid4())))
        assert conflict.value.code == "context_checkpoint_conflict"
        with pytest.raises(ValueError, match="conflict"):
            await database.save_context_checkpoint(run_id, 0, replace(candidate, summary="Changed"))
        assert await database.load_context_checkpoint(session_id) == saved

    asyncio.run(exercise())


def test_checkpoint_writes_require_a_running_owned_lease_and_complete_call(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        _, run_id, candidate = await fixture(database)
        with pytest.raises(ApplicationError, match="another"):
            await MemoryRepository(tmp_path).save_context_checkpoint(run_id, 0, candidate)
        await database.request_run_cancellation(run_id)
        with pytest.raises(ValueError, match="running"):
            await database.save_context_checkpoint(run_id, 0, candidate)

    asyncio.run(exercise())


def test_checkpoint_cannot_cover_the_current_request_or_skip_a_source(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        session_id, run_id, candidate = await fixture(database)
        current = (await database.list_messages(session_id))[-1]
        with pytest.raises(ValueError, match="current"):
            await database.save_context_checkpoint(
                run_id,
                0,
                replace(candidate, sources=(CheckpointSource(current.id, "message", "now"),)),
            )
        with pytest.raises(ValueError, match="prefix"):
            await database.save_context_checkpoint(
                run_id, 0, replace(candidate, sources=candidate.sources[1:])
            )

    asyncio.run(exercise())


def test_checkpoint_native_payload_is_preserved_byte_for_byte(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        session_id, run_id, candidate = await fixture(database)
        payload = (
            '[ { "type" : "compaction", "encrypted_content" : "opaque" }, '
            '{"type":"message","content":"kept"} ]'
        )
        saved = await database.save_context_checkpoint(
            run_id, 0, replace(candidate, native_kind="openai_window", native_payload_json=payload)
        )
        assert saved is not None and saved.draft.native_payload_json == payload
        assert (await database.load_context_checkpoint(session_id)) == saved

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "change",
    [
        {"summary": ""},
        {"summary": "x" * 32769},
        {"source_hash": "not-a-hash"},
        {"input_tokens_before": -1},
        {"native_kind": "openai_window", "native_payload_json": "{}"},
        {"native_kind": "openai_window", "native_payload_json": "x" * 262145},
        {"native_kind": "openai_window", "native_payload_json": "[NaN]"},
    ],
)
def test_checkpoint_payload_limits_are_validated_before_storage(change: dict[str, object]) -> None:
    candidate = draft(str(uuid4()), (CheckpointSource(str(uuid4()), "message", "old"),))
    with pytest.raises(ValueError):
        replace(candidate, **change)


def checkpoint_row() -> dict[str, object]:
    run = _run_row()
    candidate = draft(str(uuid4()), (CheckpointSource(str(uuid4()), "message", "old"),))
    return {
        **asdict(candidate),
        "user_id": str(ALICE),
        "chat_id": run["chat_id"],
        "run_id": run["id"],
        "revision": 1,
        "parent_id": None,
        "provider_id": "openai",
        "model_id": "openai:gpt-5.5",
        "adapter_kind": "openai",
        "covered_message_ordinal": 2,
        "covered_run_message_ordinal": 0,
        "created_at": "2026-10-15T00:00:00Z",
    }


def test_cloud_checkpoint_read_is_owned_and_bounded() -> None:
    captured: list[httpx.Request] = []
    row = checkpoint_row()

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[row])

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repo = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            result = await repo.load_context_checkpoint(str(row["chat_id"]))
            assert result is not None and result.id == row["id"]

    asyncio.run(exercise())
    assert len(captured) == 1 and captured[0].url.params["limit"] == "1"
    assert captured[0].url.params["user_id"] == f"eq.{ALICE}"
    assert captured[0].url.params["order"] == "revision.desc"


@pytest.mark.parametrize("foreign", ["user", "chat"])
def test_cloud_checkpoint_rejects_foreign_rows(foreign: str) -> None:
    row = checkpoint_row()
    chat_id = str(row["chat_id"])
    row["user_id" if foreign == "user" else "chat_id"] = str(BOB)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[row]))
        ) as client:
            repo = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            with pytest.raises(RuntimeError, match="another"):
                await repo.load_context_checkpoint(chat_id)

    asyncio.run(exercise())


def test_cloud_checkpoint_save_sends_revision_and_owned_run_capability() -> None:
    captured: list[dict[str, object]] = []
    row = checkpoint_row()
    candidate = ContextCheckpointDraft(
        str(row["id"]),
        str(row["model_call_id"]),
        (CheckpointSource(str(uuid4()), "message", "old"),),
        "Saved summary",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        1000,
        100,
    )
    row.update(asdict(candidate))

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if body["p_action"] == "create_run":
            return httpx.Response(200, json={"run": _run_row(), "lease_acquired": True})
        return httpx.Response(200, json=row)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repo = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            run = await repo.create_run(str(row["chat_id"]), "turn", "request", "Hello", _model())
            result = await repo.save_context_checkpoint(run.id, 0, candidate)
            assert result is not None and result.id == row["id"]

    asyncio.run(exercise())
    assert captured[-1]["p_action"] == "save_context_checkpoint"
    assert captured[-1]["p_capability"] == captured[0]["p_capability"]
    payload = captured[-1]["p_payload"]
    assert isinstance(payload, dict) and payload["expected_revision"] == 0
    assert payload["summary"] == "Saved summary"


def test_checkpoint_revision_keeps_pricing_high_water_mark_without_reloading_archived_sources(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        session_id, run_id, candidate = await fixture(database)
        first = await database.save_context_checkpoint(
            run_id, 0, replace(candidate, pricing_context_tokens=300000)
        )
        assert first is not None
        child = await database.save_context_checkpoint(
            run_id,
            1,
            replace(
                candidate,
                id=str(uuid4()),
                sources=(CheckpointSource(first.id, "checkpoint", "checkpoint"),),
                pricing_context_tokens=1000,
            ),
        )
        assert child is not None and child.parent_id == first.id and child.revision == 2
        assert child.draft.pricing_context_tokens == 300000
        assert [
            message.content for message in await database.list_context_messages(session_id, 0)
        ] == ["Continue"]

    asyncio.run(exercise())


def test_context_window_pairs_one_checkpoint_revision_with_its_unarchived_messages(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        session_id, run_id, candidate = await fixture(database)
        saved = await database.save_context_checkpoint(run_id, 0, candidate)
        window = await MemoryRepository(tmp_path).load_context_window(session_id)
        assert window.checkpoint == saved
        assert [message.content for message in window.messages] == ["Continue"]

    asyncio.run(exercise())


def test_cloud_context_window_uses_one_owned_read_snapshot() -> None:
    row = checkpoint_row()
    chat_id = str(row["chat_id"])
    message = {
        "id": str(uuid4()),
        "user_id": str(ALICE),
        "chat_id": chat_id,
        "turn_id": "now",
        "ordinal": 3,
        "role": "user",
        "content": "Continue",
        "provider": None,
        "model": None,
        "created_at": "2026-10-15T00:00:00Z",
    }
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"checkpoint": row, "messages": [message]})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repo = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            window = await repo.load_context_window(chat_id)
            assert window.checkpoint is not None and window.checkpoint.id == row["id"]
            assert window.messages[0].content == "Continue"

    asyncio.run(exercise())
    assert len(requests) == 1 and requests[0].url.path == "/rest/v1/rpc/read_context_window"
    assert json.loads(requests[0].content) == {"p_session_id": chat_id, "p_limit": 128}


def test_same_run_checkpoint_advances_its_run_message_cursor_without_rehydration(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        from app.domain.runtime import ModelMessage

        database = MemoryRepository(tmp_path)
        _, run_id, candidate = await fixture(database)
        first_message, _, _ = await database.record_assistant_message(
            run_id, candidate.model_call_id, ModelMessage("assistant", "Earlier completed work")
        )
        second_message, _, _ = await database.record_assistant_message(
            run_id, candidate.model_call_id, ModelMessage("assistant", "Later completed work")
        )
        turn_id = database.state.runs[run_id].turn_id
        first = await database.save_context_checkpoint(
            run_id,
            0,
            replace(
                candidate,
                sources=(
                    *candidate.sources,
                    CheckpointSource(first_message.id, "run_message", turn_id),
                ),
            ),
        )
        child = await database.save_context_checkpoint(
            run_id,
            1,
            replace(
                candidate,
                id=str(uuid4()),
                sources=(
                    CheckpointSource(first.id, "checkpoint", "checkpoint"),
                    CheckpointSource(second_message.id, "run_message", turn_id),
                ),
            ),
        )
        assert child.covered_run_message_ordinal == 2
        with pytest.raises(ValueError, match="archived"):
            await database.save_context_checkpoint(
                run_id,
                2,
                replace(
                    candidate,
                    id=str(uuid4()),
                    sources=(
                        CheckpointSource(child.id, "checkpoint", "checkpoint"),
                        CheckpointSource(first_message.id, "run_message", turn_id),
                    ),
                ),
            )

    asyncio.run(exercise())
