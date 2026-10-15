import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from app.domain.history import PageCursor
from app.infrastructure.supabase_repository import SupabaseRepository
from tests.memory_repository import MemoryRepository
from tests.support import verified_token_source
from tests.test_supabase_repository import ALICE, BOB, SETTINGS


def test_cloud_history_uses_one_bounded_owned_rpc_and_preserves_cursor(tmp_path: Path) -> None:
    async def exercise():
        memory = MemoryRepository(tmp_path / "db", owner_id=ALICE)
        session = await memory.create_session()
        message = await memory.add_user_message(session.id, "turn", "Saved question")
        row = asdict(message)
        row["user_id"] = str(ALICE)
        row["chat_id"] = row.pop("session_id")
        cursor = PageCursor(message.created_at, "message", message.id)
        captured = []

        def respond(request):
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "messages": [row],
                    "assistant_outputs": [],
                    "timeline_order": [{"kind": "message", "id": message.id}],
                    "next_before": asdict(cursor),
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            page = await repository.read_history_page(session.id, None, limit=3)
            assert page.messages == (message,)
            assert page.next_before_cursor == cursor.encode()
            assert len(captured) == 1
            assert captured[0].url.path == "/rest/v1/rpc/read_chat_history"
            assert json.loads(captured[0].content)["p_limit"] == 3
            assert json.loads(captured[0].content)["p_chat_id"] == session.id

    asyncio.run(exercise())


@pytest.mark.parametrize("corruption", ["owner", "chat", "order", "duplicate"])
def test_cloud_history_rejects_foreign_or_inconsistent_rows(
    tmp_path: Path, corruption: str
) -> None:
    async def exercise():
        memory = MemoryRepository(tmp_path / "db", owner_id=ALICE)
        session = await memory.create_session()
        message = await memory.add_user_message(session.id, "turn", "Saved question")
        row = asdict(message)
        row["user_id"] = str(BOB if corruption == "owner" else ALICE)
        row["chat_id"] = str(BOB) if corruption == "chat" else row.pop("session_id")
        order = [{"kind": "message", "id": message.id}]
        if corruption == "order":
            order[0]["id"] = str(BOB)
        if corruption == "duplicate":
            order *= 2
        payload = {
            "messages": [row],
            "assistant_outputs": [],
            "timeline_order": order,
            "next_before": None,
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            with pytest.raises(RuntimeError):
                await repository.read_history_page(session.id, None, limit=3)

    asyncio.run(exercise())
