"""A test repository can exercise account wiring without a hosted database."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.infrastructure.accounts import AccountRegistry
from app.infrastructure.supabase_repository import VerifiedTokenSource
from app.main import create_app
from tests.memory_repository import MemoryRepository
from tests.support import StubVerifier


def test_registry_uses_injected_repository_factory(tmp_path: Path) -> None:
    user_id = UUID("c928705a-6f03-4aa8-9f81-c4fb50696527")
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    calls: list[tuple[Settings, UUID, VerifiedTokenSource, httpx.AsyncClient]] = []

    repository = MemoryRepository(tmp_path / "account", owner_id=user_id)

    def factory(
        supplied_settings: Settings,
        supplied_user_id: UUID,
        token_source: VerifiedTokenSource,
        client: httpx.AsyncClient,
    ) -> MemoryRepository:
        calls.append((supplied_settings, supplied_user_id, token_source, client))
        return repository

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = AccountRegistry(
                settings, {}, {}, cloud_client=client, repository_factory=factory
            )
            try:
                context = await registry.get(
                    user_id,
                    access_token="verified-test-token",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                assert context.database is repository
                assert len(calls) == 1
                assert calls[0][0] is settings
                assert calls[0][1] == user_id
                assert calls[0][2].get_token() == "verified-test-token"
                assert calls[0][3] is client
            finally:
                await registry.close()

    asyncio.run(exercise())


def test_create_app_forwards_repository_factory(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    created: list[UUID] = []

    def factory(
        _settings: Settings,
        user_id: UUID,
        _tokens: VerifiedTokenSource,
        _client: httpx.AsyncClient,
    ) -> MemoryRepository:
        created.append(user_id)
        return MemoryRepository(tmp_path / str(user_id), owner_id=user_id)

    app = create_app(settings, auth_verifier=StubVerifier(), repository_factory=factory)
    with TestClient(app) as client:
        response = client.get("/api/profile", headers={"Authorization": "Bearer alice"})
    assert response.status_code == 200
    assert response.json()["id"] == str(created[0])
