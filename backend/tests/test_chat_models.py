import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.application.sessions import SessionService
from app.domain.runtime import ModelStreamEvent, RunEventType, RunStatus
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MODELS, MemoryRepository
from tests.support import account_context
from tests.test_runtime_api import StreamingProvider, configured_client
from tests.test_runtime_service import RecordingProvider


def test_chat_model_http_contract_and_active_run_lock(tmp_path: Path) -> None:
    with configured_client(tmp_path, StreamingProvider()) as client:
        chat = client.post("/api/sessions").json()
        assert chat["model_id"] == "openai:gpt-5.5"
        selected = client.put(
            f"/api/sessions/{chat['id']}/model", json={"model_id": "anthropic:claude-sonnet-5"}
        )
        assert (
            selected.status_code == 200
            and selected.json()["model_id"] == "anthropic:claude-sonnet-5"
        )
        assert (
            client.get(f"/api/sessions/{chat['id']}").json()["session"]["model_id"]
            == "anthropic:claude-sonnet-5"
        )
        assert client.get("/api/settings").json()["selected_model_id"] == "openai:gpt-5.5"
        database = account_context(client.app).database

        async def create_run():
            return await database.create_run(
                chat["id"], "locked", "locked", "Hi", (await database.list_models())[0]
            )

        asyncio.run(create_run())
        locked = client.put(
            f"/api/sessions/{chat['id']}/model", json={"model_id": "openai:gpt-5.5"}
        )
        assert locked.status_code == 409 and locked.json()["error"]["code"] == "session_model_busy"


def test_chat_model_is_seeded_once_and_isolated_from_other_chats(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path)
        first = await database.create_session()
        await database.set_selected_model("anthropic:claude-sonnet-5")
        second = await database.create_session()
        assert first.model_id == "openai:gpt-5.5"
        assert second.model_id == "anthropic:claude-sonnet-5"
        updated = await SessionService(database).select_model(first.id, "anthropic:claude-sonnet-5")
        assert updated.model_id == "anthropic:claude-sonnet-5"
        saved_second = await database.get_session(second.id)
        assert saved_second is not None and saved_second.model_id == second.model_id
        assert await database.get_selected_model_id() == second.model_id

    asyncio.run(exercise())


def test_chat_model_is_locked_for_an_active_run(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path)
        session = await database.create_session()
        run = await database.create_run(
            session.id, "turn", "request", "Hi", (await database.list_models())[0]
        )
        with pytest.raises(ApplicationError, match="finish") as caught:
            await SessionService(database).select_model(session.id, "anthropic:claude-sonnet-5")
        assert caught.value.code == "session_model_busy"
        saved = await database.get_session(session.id)
        assert saved is not None and saved.model_id == "openai:gpt-5.5"
        await database.transition_run_record(
            run.id, RunStatus.CANCELLED, RunEventType.CANCELLED, {}
        )
        database.state.models.append(
            replace(MODELS[0], id="unknown:pending", upstream_model_id="pending")
        )
        with pytest.raises(ApplicationError) as unavailable:
            await SessionService(database).select_model(session.id, "unknown:pending")
        assert unavailable.value.code == "model_not_available"

    asyncio.run(exercise())


def test_chat_model_selection_does_not_require_positive_provider_availability(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        database.state.models = [replace(MODELS[1], availability="unavailable")]
        session = await database.create_session()
        updated = await SessionService(database).select_model(
            session.id, "anthropic:claude-sonnet-5"
        )
        assert updated.model_id == "anthropic:claude-sonnet-5"
        saved = await database.get_session(session.id)
        assert saved is not None and saved.model_id == "anthropic:claude-sonnet-5"
        assert await database.get_selected_model_id() == "openai:gpt-5.5"

    asyncio.run(exercise())


def test_run_uses_chat_preference_and_locks_explicit_selection(tmp_path: Path) -> None:
    async def exercise():
        database = MemoryRepository(tmp_path / "memory")
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        await secrets.set("anthropic", "test-key")
        session = await database.create_session()
        await SessionService(database).select_model(session.id, "anthropic:claude-sonnet-5")
        provider = RecordingProvider(
            (ModelStreamEvent(kind="text_delta", text="Done"), ModelStreamEvent(kind="completed"))
        )
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider, "anthropic": provider},
        )
        try:
            run = await service.create_run(session.id, "first", "Hi")
            await service.wait_for_run(run.id)
            assert run.model_id == "anthropic:claude-sonnet-5"
            explicit = await service.create_run(
                session.id, "second", "Hello", model_id="openai:gpt-5.5"
            )
            await service.wait_for_run(explicit.id)
            assert explicit.model_id == "openai:gpt-5.5"
            saved = await database.get_session(session.id)
            assert saved is not None and saved.model_id == "anthropic:claude-sonnet-5"
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("availability", ["unavailable", "unknown"])
def test_reviewed_chat_choice_dispatches_exact_model_without_positive_availability(
    tmp_path: Path, availability: str
) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "memory")
        model = replace(
            MODELS[0],
            id="openai:gpt-6-luna",
            upstream_model_id="gpt-6-luna",
            name="GPT-6 Luna",
            availability=availability,
        )
        database.state.models.append(model)
        catalog_before = tuple(database.state.models)
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        session = await database.create_session()
        await SessionService(database).select_model(session.id, "openai:gpt-6-luna")
        provider = RecordingProvider(
            (
                ModelStreamEvent(kind="text_delta", text="Done"),
                ModelStreamEvent(
                    kind="usage",
                    input_tokens=1,
                    output_tokens=1,
                    cached_tokens=0,
                    cache_creation_tokens=0,
                    service_tier="standard",
                ),
                ModelStreamEvent(kind="completed"),
            )
        )
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(session.id, "first", "Hi")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None
            assert saved.status is RunStatus.COMPLETED, (saved.error_code, saved.error_message)
            assert saved.model_id == "openai:gpt-6-luna"
            assert provider.request is not None
            assert provider.request.model_id == "openai:gpt-6-luna"
            assert provider.request.upstream_model_id == "gpt-6-luna"
            assert tuple(database.state.models) == catalog_before
        finally:
            await service.close()

    asyncio.run(exercise())
