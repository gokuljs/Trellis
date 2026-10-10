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
BOB = UUID("55114a4d-aa42-4eb6-a61d-f91c7992c06d")


class StubRepository:
    def __init__(
        self, _settings: Settings, _user_id: UUID, _source: VerifiedTokenSource, _client: object
    ) -> None:
        pass

    async def ensure_account(self) -> None:
        pass

    async def list_decided_approval_runs(self) -> list[str]:
        return []


def test_existing_account_remains_available_during_another_accounts_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_started = asyncio.Event()
    allow_import = asyncio.Event()

    async def controlled_import(_directory: Path, user_id: UUID, _lock: int, _sink: object) -> None:
        if user_id == ALICE:
            import_started.set()
            await allow_import.wait()

    monkeypatch.setattr(accounts, "SupabaseRepository", StubRepository)
    monkeypatch.setattr(accounts, "import_locked_account", controlled_import)
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            expires = datetime.now(UTC) + timedelta(hours=1)
            first = await registry.get(BOB, access_token="bob-first", expires_at=expires)
            alice_task = asyncio.create_task(
                registry.get(ALICE, access_token="alice", expires_at=expires)
            )
            try:
                await asyncio.wait_for(import_started.wait(), timeout=1)
                existing = await asyncio.wait_for(
                    registry.get(BOB, access_token="bob-second", expires_at=expires),
                    timeout=0.5,
                )
                assert existing is first
            finally:
                allow_import.set()
                await alice_task
                await registry.close()

    asyncio.run(exercise())


def test_concurrent_requests_initialize_one_account_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_started = asyncio.Event()
    allow_import = asyncio.Event()
    imports = 0

    async def controlled_import(
        _directory: Path, _user_id: UUID, _lock: int, _sink: object
    ) -> None:
        nonlocal imports
        imports += 1
        import_started.set()
        await allow_import.wait()

    monkeypatch.setattr(accounts, "SupabaseRepository", StubRepository)
    monkeypatch.setattr(accounts, "import_locked_account", controlled_import)
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            expires = datetime.now(UTC) + timedelta(hours=1)
            first_task = asyncio.create_task(
                registry.get(ALICE, access_token="first", expires_at=expires)
            )
            try:
                await asyncio.wait_for(import_started.wait(), timeout=1)
                second_task = asyncio.create_task(
                    registry.get(ALICE, access_token="second", expires_at=expires)
                )
                await asyncio.sleep(0)
                allow_import.set()
                first, second = await asyncio.gather(first_task, second_task)
                assert first is second
                assert imports == 1
            finally:
                allow_import.set()
                if not first_task.done():
                    await first_task
                await registry.close()

    asyncio.run(exercise())


def test_cancelled_import_releases_its_account_lock_for_waiting_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_started = asyncio.Event()
    allow_import = asyncio.Event()
    imports = 0

    async def controlled_import(
        _directory: Path, _user_id: UUID, _lock: int, _sink: object
    ) -> None:
        nonlocal imports
        imports += 1
        import_started.set()
        await allow_import.wait()

    monkeypatch.setattr(accounts, "SupabaseRepository", StubRepository)
    monkeypatch.setattr(accounts, "import_locked_account", controlled_import)
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            expires = datetime.now(UTC) + timedelta(hours=1)
            first_task = asyncio.create_task(
                registry.get(ALICE, access_token="first", expires_at=expires)
            )
            try:
                await asyncio.wait_for(import_started.wait(), timeout=1)
                waiting_task = asyncio.create_task(
                    registry.get(ALICE, access_token="second", expires_at=expires)
                )
                await asyncio.sleep(0)
                first_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first_task
                allow_import.set()
                context = await asyncio.wait_for(waiting_task, timeout=1)
                assert context.database is not None
                assert imports == 2
            finally:
                allow_import.set()
                await registry.close()

    asyncio.run(exercise())


def test_close_waits_for_import_before_releasing_account_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_started = asyncio.Event()
    allow_import = asyncio.Event()

    async def controlled_import(
        _directory: Path, _user_id: UUID, _lock: int, _sink: object
    ) -> None:
        import_started.set()
        await allow_import.wait()

    monkeypatch.setattr(accounts, "SupabaseRepository", StubRepository)
    monkeypatch.setattr(accounts, "import_locked_account", controlled_import)
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            expires = datetime.now(UTC) + timedelta(hours=1)
            initial = asyncio.create_task(
                registry.get(ALICE, access_token="first", expires_at=expires)
            )
            await asyncio.wait_for(import_started.wait(), timeout=1)
            close_task = asyncio.create_task(registry.close())
            await asyncio.sleep(0)
            assert not close_task.done()
            allow_import.set()
            with pytest.raises(RuntimeError, match="closed"):
                await initial
            await close_task

            reopened = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            try:
                context = await reopened.get(ALICE, access_token="second", expires_at=expires)
                assert context.database is not None
            finally:
                await reopened.close()

    asyncio.run(exercise())
