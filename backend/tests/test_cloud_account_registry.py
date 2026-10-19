import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.application.telemetry import NO_TELEMETRY, Telemetry
from app.core.config import Settings
from app.infrastructure import accounts
from app.infrastructure.supabase_repository import VerifiedTokenSource

ALICE = UUID("c928705a-6f03-4aa8-9f81-c4fb50696527")


def test_cloud_registry_initializes_account_and_refreshes_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    sources: list[VerifiedTokenSource] = []

    class FakeRepository:
        def __init__(
            self,
            _settings: Settings,
            _user_id: UUID,
            source: VerifiedTokenSource,
            _client: object,
            *,
            telemetry: Telemetry = NO_TELEMETRY,
        ):
            sources.append(source)
            self.telemetry = telemetry

        async def ensure_account(self) -> None:
            events.append("ensure")

        async def list_decided_approval_runs(self) -> list[str]:
            return []

    monkeypatch.setattr(accounts, "SupabaseRepository", FakeRepository)
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
                assert events == ["ensure"]
                assert len(sources) == 1
                assert first.database is not None
                assert first.run_service._workspace_bindings is None
                assert first.session_service._workspace_bindings is None
                assert first.run_service._lease_repository is first.database
                assert sources[0].get_token() == "second"
            finally:
                await registry.close()

    asyncio.run(exercise())


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
