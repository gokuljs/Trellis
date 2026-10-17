import asyncio
from pathlib import Path

import pytest

from tests.memory_repository import MemoryRepository
from tests.support import (
    TEST_USER_ID,
    TestClient,
    account_repository_key,
    create_app,
    make_settings,
)


def test_history_pages_recover_every_saved_message_with_bounded_rows(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = MemoryRepository(account_repository_key(settings), owner_id=TEST_USER_ID)

    async def seed():
        session = await database.create_session()
        for index in range(12):
            await database.add_user_message(session.id, f"turn{index}", f"Question {index}")
            await database.add_assistant_message(
                session.id, f"turn{index}", f"Answer {index}", "openai", "openai:gpt-5.5"
            )
        return session.id

    session_id = asyncio.run(seed())
    with TestClient(create_app(settings)) as client:
        cursor = None
        seen = []
        while True:
            params = {"limit": 3}
            if cursor is not None:
                params["before_cursor"] = cursor
            response = client.get(f"/api/sessions/{session_id}/history", params=params)
            assert response.status_code == 200
            page = response.json()
            assert len(page["timeline_order"]) <= 3
            seen.extend(row["id"] for row in page["timeline_order"])
            cursor = page["next_before_cursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 24
        legacy = client.get(f"/api/sessions/{session_id}").json()
        assert set(seen) == {row["id"] for row in legacy["messages"]}


def test_session_pages_are_bounded_and_have_stable_cursors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = MemoryRepository(account_repository_key(settings), owner_id=TEST_USER_ID)

    async def seed():
        for _ in range(11):
            await database.create_session()

    asyncio.run(seed())
    with TestClient(create_app(settings)) as client:
        cursor = None
        seen = []
        while True:
            params = {"limit": 3}
            if cursor is not None:
                params["before_cursor"] = cursor
            response = client.get("/api/session-pages", params=params)
            assert response.status_code == 200
            page = response.json()
            assert len(page["items"]) <= 3
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_before_cursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 11


@pytest.mark.parametrize("cursor", ["garbage", "e30=", "x" * 513])
def test_history_rejects_invalid_cursors_without_leaking_them(tmp_path: Path, cursor: str) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        session_id = client.post("/api/sessions").json()["id"]
        result = client.get(f"/api/sessions/{session_id}/history", params={"before_cursor": cursor})
        assert result.status_code in {400, 422}
        assert cursor not in result.text


def test_history_rejects_unknown_chat_and_invalid_limits(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        assert client.get("/api/sessions/missing/history").status_code == 404
        session_id = client.post("/api/sessions").json()["id"]
        for limit in [0, 101]:
            assert (
                client.get(
                    f"/api/sessions/{session_id}/history", params={"limit": limit}
                ).status_code
                == 422
            )
        client.headers.pop("Authorization")
        assert client.get(f"/api/sessions/{session_id}/history").status_code == 401


def test_partial_output_pages_retain_retry_input_without_loading_the_full_chat(
    tmp_path: Path,
) -> None:
    from app.domain.runtime import ModelCallStatus, RunEventType, RunStatus

    settings = make_settings(tmp_path)
    database = MemoryRepository(account_repository_key(settings), owner_id=TEST_USER_ID)

    async def seed():
        session = await database.create_session()
        model = (await database.list_models())[0]
        run = await database.create_run(session.id, "turn", "request", "Accepted request", model)
        await database.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})
        for index in range(6):
            call = await database.create_model_call(run.id, index + 1, model, {})
            await database.update_model_call(call.id, ModelCallStatus.STREAMING)
            await database.append_assistant_batch(run.id, call.id, 0, f"Working note {index}")
            await database.finish_assistant_output(run.id, call.id)
        await database.request_run_cancellation(run.id)
        await database.transition_run_record(
            run.id, RunStatus.CANCELLED, RunEventType.CANCELLED, {}
        )
        return session.id

    session_id = asyncio.run(seed())
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/api/sessions/{session_id}/history", params={"limit": 3}).json()
        assert len(page["timeline_order"]) == len(page["assistant_outputs"]) == 3
        assert page["messages"] == []
        assert all(row["kind"] == "assistant_output" for row in page["timeline_order"])
        restored = client.get(f"/api/sessions/{session_id}/runs/latest").json()
        assert restored["input_message"]["content"] == "Accepted request"
        assert restored["input_message"]["turn_id"] == "turn"
        older = client.get(
            f"/api/sessions/{session_id}/history",
            params={"limit": 3, "before_cursor": page["next_before_cursor"]},
        ).json()
        assert {row["id"] for row in page["timeline_order"]}.isdisjoint(
            row["id"] for row in older["timeline_order"]
        )


def test_activity_pages_can_read_one_turn_without_loading_other_attempts(tmp_path: Path) -> None:
    from tests.test_runtime_api import StreamingProvider, configured_client, start_completed_run

    with configured_client(tmp_path, StreamingProvider()) as client:
        session_id = client.post("/api/sessions").json()["id"]
        older = start_completed_run(client, session_id, "older")
        newer = start_completed_run(client, session_id, "newer")
        page = client.get(
            f"/api/sessions/{session_id}/runs", params={"limit": 1, "newest_first": True}
        ).json()
        assert page["items"][0]["run_id"] == newer
        turn_id = page["items"][0]["turn_id"]
        isolated = client.get(
            f"/api/sessions/{session_id}/runs", params={"turn_id": turn_id, "newest_first": True}
        ).json()
        assert [item["run_id"] for item in isolated["items"]] == [newer]
        assert older != newer and isolated["next_offset"] is None
