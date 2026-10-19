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
        self, _settings: Settings, user_id: UUID, _source: VerifiedTokenSource, _client: object
    ) -> None:
        self.user_id = user_id

    async def ensure_account(self) -> None:
        pass

    async def list_decided_approval_runs(self) -> list[str]:
        return []


def test_existing_account_remains_available_during_another_accounts_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialization_started = asyncio.Event()
    allow_initialization = asyncio.Event()

    class ControlledRepository(StubRepository):
        async def ensure_account(self) -> None:
            if self.user_id == ALICE:
                initialization_started.set()
                await allow_initialization.wait()

    monkeypatch.setattr(accounts, "SupabaseRepository", ControlledRepository)
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
                await asyncio.wait_for(initialization_started.wait(), timeout=1)
                existing = await asyncio.wait_for(
                    registry.get(BOB, access_token="bob-second", expires_at=expires),
                    timeout=0.5,
                )
                assert existing is first
            finally:
                allow_initialization.set()
                await alice_task
                await registry.close()

    asyncio.run(exercise())


def test_concurrent_requests_initialize_one_account_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialization_started = asyncio.Event()
    allow_initialization = asyncio.Event()
    initializations = 0

    class ControlledRepository(StubRepository):
        async def ensure_account(self) -> None:
            nonlocal initializations
            initializations += 1
            initialization_started.set()
            await allow_initialization.wait()

    monkeypatch.setattr(accounts, "SupabaseRepository", ControlledRepository)
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            expires = datetime.now(UTC) + timedelta(hours=1)
            first_task = asyncio.create_task(
                registry.get(ALICE, access_token="first", expires_at=expires)
            )
            try:
                await asyncio.wait_for(initialization_started.wait(), timeout=1)
                second_task = asyncio.create_task(
                    registry.get(ALICE, access_token="second", expires_at=expires)
                )
                await asyncio.sleep(0)
                allow_initialization.set()
                first, second = await asyncio.gather(first_task, second_task)
                assert first is second
                assert initializations == 1
            finally:
                allow_initialization.set()
                if not first_task.done():
                    await first_task
                await registry.close()

    asyncio.run(exercise())


def test_cancelled_initialization_releases_its_account_lock_for_waiting_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialization_started = asyncio.Event()
    allow_initialization = asyncio.Event()
    initializations = 0

    class ControlledRepository(StubRepository):
        async def ensure_account(self) -> None:
            nonlocal initializations
            initializations += 1
            initialization_started.set()
            await allow_initialization.wait()

    monkeypatch.setattr(accounts, "SupabaseRepository", ControlledRepository)
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            expires = datetime.now(UTC) + timedelta(hours=1)
            first_task = asyncio.create_task(
                registry.get(ALICE, access_token="first", expires_at=expires)
            )
            try:
                await asyncio.wait_for(initialization_started.wait(), timeout=1)
                waiting_task = asyncio.create_task(
                    registry.get(ALICE, access_token="second", expires_at=expires)
                )
                await asyncio.sleep(0)
                first_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first_task
                allow_initialization.set()
                context = await asyncio.wait_for(waiting_task, timeout=1)
                assert context.database is not None
                assert initializations == 2
            finally:
                allow_initialization.set()
                await registry.close()

    asyncio.run(exercise())


def test_close_waits_for_account_initialization_before_releasing_account_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialization_started = asyncio.Event()
    allow_initialization = asyncio.Event()

    class ControlledRepository(StubRepository):
        async def ensure_account(self) -> None:
            initialization_started.set()
            await allow_initialization.wait()

    monkeypatch.setattr(accounts, "SupabaseRepository", ControlledRepository)
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            registry = accounts.AccountRegistry(settings, {}, {}, cloud_client=client)
            expires = datetime.now(UTC) + timedelta(hours=1)
            initial = asyncio.create_task(
                registry.get(ALICE, access_token="first", expires_at=expires)
            )
            await asyncio.wait_for(initialization_started.wait(), timeout=1)
            close_task = asyncio.create_task(registry.close())
            await asyncio.sleep(0)
            assert not close_task.done()
            allow_initialization.set()
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
