import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.core.config import Settings
from app.infrastructure import accounts
from app.infrastructure.supabase_repository import VerifiedTokenSource

ALICE = UUID("c928705a-6f03-4aa8-9f81-c4fb50696527")


def test_cloud_registry_imports_before_initializing_account_and_refreshes_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    sources: list[VerifiedTokenSource] = []

    class FakeRepository:
        def __init__(
            self, _settings: Settings, _user_id: UUID, source: VerifiedTokenSource, _client: object
        ):
            sources.append(source)

        async def ensure_account(self) -> None:
            events.append("ensure")

        async def list_decided_approval_runs(self) -> list[str]:
            return []

    async def fake_import(_directory: Path, _user_id: UUID, _lock: int, _sink: object) -> None:
        events.append("import")

    monkeypatch.setattr(accounts, "SupabaseRepository", FakeRepository)
    monkeypatch.setattr(accounts, "import_locked_account", fake_import)
    settings = Settings(
        environment="test",
        data_dir=tmp_path / "data",
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="sb_publishable_test",
    )

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            try:
                expires = datetime.now(UTC) + timedelta(hours=1)
                first = await registry.get(ALICE, access_token="first", expires_at=expires)
                assert await registry.get(ALICE, access_token="second", expires_at=expires) is first
                assert events == ["import", "ensure"]
                assert len(sources) == 1
                assert first.database is not None
                assert (
                    first.run_service._workspace_bindings
                    is first.session_service._workspace_bindings
                )
                assert first.run_service._lease_repository is first.database
                assert sources[0].get_token() == "second"
            finally:
                await registry.close()

    asyncio.run(exercise())
    assert not (settings.data_dir / "accounts" / str(ALICE) / "state.db").exists()


def test_cloud_registry_requires_fresh_verified_token(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path)

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            try:
                with pytest.raises(RuntimeError, match="verified token"):
                    await registry.get(ALICE)
            finally:
                await registry.close()

    asyncio.run(exercise())
