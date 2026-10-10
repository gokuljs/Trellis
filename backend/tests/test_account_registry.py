import asyncio
import os
import stat
from pathlib import Path
from typing import Never
from uuid import UUID

import pytest

from app.application.errors import ApplicationError
from app.core.config import Settings
from app.domain.runtime import RunStatus
from app.infrastructure.accounts import AccountRegistry
from app.infrastructure.database import Database

ALICE = UUID("c928705a-6f03-4aa8-9f81-c4fb50696527")
BOB = UUID("55114a4d-aa42-4eb6-a61d-f91c7992c06d")


def test_registry_separates_user_data_and_provider_keys(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    global_database = settings.database_path
    global_database.parent.mkdir(parents=True)
    global_database.write_bytes(b"old-global-database")

    async def exercise() -> None:
        registry = AccountRegistry(settings, {}, {})
        try:
            alice = await registry.get(ALICE)
            bob = await registry.get(BOB)
            assert await registry.get(ALICE) is alice
            assert alice is not bob
            assert (await alice.profile_service.get()).id == str(ALICE)
            assert (await bob.profile_service.get()).id == str(BOB)

            session = await alice.session_service.create()
            await alice.secret_store.set("openai", "sk-alice-private")

            assert [item.id for item in await alice.session_service.list_sessions()] == [session.id]
            assert await bob.session_service.list_sessions() == []
            assert await bob.secret_store.get("openai") is None
            assert await alice.secret_store.get("openai") == "sk-alice-private"
        finally:
            await registry.close()

    asyncio.run(exercise())

    alice_dir = settings.data_dir / "accounts" / str(ALICE)
    bob_dir = settings.data_dir / "accounts" / str(BOB)
    assert global_database.read_bytes() == b"old-global-database"
    assert (alice_dir / "state.db").is_file()
    assert (bob_dir / "state.db").is_file()
    assert stat.S_IMODE((settings.data_dir / "accounts").stat().st_mode) == 0o700
    assert stat.S_IMODE(alice_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((alice_dir / "state.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((alice_dir / ".env").stat().st_mode) == 0o600


def test_registry_restores_only_the_matching_users_database(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        first = AccountRegistry(settings, {}, {})
        try:
            alice = await first.get(ALICE)
            saved = await alice.session_service.create()
        finally:
            await first.close()

        restarted = AccountRegistry(settings, {}, {})
        try:
            alice_again = await restarted.get(ALICE)
            bob = await restarted.get(BOB)
            assert [item.id for item in await alice_again.session_service.list_sessions()] == [
                saved.id
            ]
            assert await bob.session_service.list_sessions() == []
        finally:
            await restarted.close()

    asyncio.run(exercise())


def test_database_rejects_existing_profile_owned_by_another_user(tmp_path: Path) -> None:
    path = tmp_path / "state.db"

    async def exercise() -> None:
        await Database(path, owner_id=ALICE).initialize()
        with pytest.raises(RuntimeError, match="owner"):
            await Database(path, owner_id=BOB).initialize()
        assert (await Database(path).get_profile()).id == str(ALICE)

    asyncio.run(exercise())


def test_registry_rejects_symlinked_account_secret_file(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    account_dir = settings.data_dir / "accounts" / str(ALICE)
    account_dir.parent.mkdir(parents=True, mode=0o700)
    account_dir.mkdir(mode=0o700)
    old_secret = settings.data_dir / ".env"
    old_secret.write_text("OPENAI_API_KEY=old-global-secret\n")
    (account_dir / ".env").symlink_to(old_secret)

    async def exercise() -> None:
        registry = AccountRegistry(settings, {}, {})
        try:
            with pytest.raises(RuntimeError, match="symlink"):
                await registry.get(ALICE)
        finally:
            await registry.close()

    asyncio.run(exercise())
    assert old_secret.read_text() == "OPENAI_API_KEY=old-global-secret\n"


def test_registry_rejects_new_accounts_after_close(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        registry = AccountRegistry(settings, {}, {})
        await registry.close()
        with pytest.raises(RuntimeError, match="closed"):
            await registry.get(ALICE)

    asyncio.run(exercise())


def test_registry_preserves_sticky_shared_data_directory_mode(tmp_path: Path) -> None:
    shared_data_dir = tmp_path / "shared-data"
    shared_data_dir.mkdir(mode=0o700)
    shared_data_dir.chmod(0o1777)
    settings = Settings(environment="test", data_dir=shared_data_dir)

    async def exercise() -> None:
        registry = AccountRegistry(settings, {}, {})
        try:
            alice = await registry.get(ALICE)
            assert (await alice.profile_service.get()).id == str(ALICE)
        finally:
            await registry.close()

    asyncio.run(exercise())
    assert stat.S_IMODE(shared_data_dir.stat().st_mode) == 0o1777
    assert stat.S_IMODE((shared_data_dir / "accounts").stat().st_mode) == 0o700


def test_registry_rejects_nonsticky_world_writable_data_directory(tmp_path: Path) -> None:
    shared_data_dir = tmp_path / "unsafe-data"
    shared_data_dir.mkdir(mode=0o700)
    shared_data_dir.chmod(0o777)
    settings = Settings(environment="test", data_dir=shared_data_dir)

    async def exercise() -> None:
        registry = AccountRegistry(settings, {}, {})
        try:
            with pytest.raises(RuntimeError, match="unsafe shared directory"):
                await registry.get(ALICE)
        finally:
            await registry.close()

    asyncio.run(exercise())
    assert stat.S_IMODE(shared_data_dir.stat().st_mode) == 0o777


@pytest.mark.parametrize("mode", [0o700, 0o1777])
def test_registry_rejects_data_root_owned_by_untrusted_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    data_dir = tmp_path / "foreign-owned"
    data_dir.mkdir(mode=0o700)
    data_dir.chmod(mode)
    settings = Settings(environment="test", data_dir=data_dir)
    original_stat = Path.stat

    def foreign_owner_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path != data_dir:
            return result
        values = list(result)
        values[4] = result.st_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", foreign_owner_stat)

    async def exercise() -> None:
        registry = AccountRegistry(settings, {}, {})
        try:
            with pytest.raises(RuntimeError, match="owned"):
                await registry.get(ALICE)
        finally:
            await registry.close()

    asyncio.run(exercise())


def test_registry_allows_root_owned_sticky_shared_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "shared-root"
    data_dir.mkdir(mode=0o700)
    data_dir.chmod(0o1777)
    settings = Settings(environment="test", data_dir=data_dir)
    original_stat = Path.stat

    def root_owner_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path != data_dir:
            return result
        values = list(result)
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", root_owner_stat)

    async def exercise() -> None:
        registry = AccountRegistry(settings, {}, {})
        try:
            alice = await registry.get(ALICE)
            assert (await alice.profile_service.get()).id == str(ALICE)
        finally:
            await registry.close()

    asyncio.run(exercise())


def test_live_account_cannot_be_recovered_by_second_registry(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        first = AccountRegistry(settings, {}, {})
        second = AccountRegistry(settings, {}, {})
        try:
            alice = await first.get(ALICE)
            session = await alice.session_service.create()
            model = (await alice.database.list_models())[0]
            run = await alice.database.create_run(
                session.id, "turn-1", "request-1", "Keep running", model
            )
            assert run.status is RunStatus.QUEUED

            with pytest.raises(ApplicationError) as conflict:
                await second.get(ALICE)
            assert conflict.value.code == "account_in_use"
            still_active = await alice.database.get_run(run.id)
            assert still_active is not None
            assert still_active.status is RunStatus.QUEUED

            bob = await second.get(BOB)
            assert (await bob.profile_service.get()).id == str(BOB)

            await first.close()
            recovered = await second.get(ALICE)
            restored_run = await recovered.database.get_run(run.id)
            assert restored_run is not None
            assert restored_run.status is RunStatus.INTERRUPTED
        finally:
            await first.close()
            await second.close()

    asyncio.run(exercise())


def test_failed_account_setup_releases_lock(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    def fail_to_build_tools(_database: Database) -> Never:
        raise RuntimeError("tool setup failed")

    async def exercise() -> None:
        failing = AccountRegistry(settings, {}, {}, tool_registry_factory=fail_to_build_tools)
        healthy = AccountRegistry(settings, {}, {})
        try:
            with pytest.raises(RuntimeError, match="tool setup failed"):
                await failing.get(ALICE)
            alice = await healthy.get(ALICE)
            assert (await alice.profile_service.get()).id == str(ALICE)
        finally:
            await failing.close()
            await healthy.close()

    asyncio.run(exercise())


def test_cancelled_registry_close_keeps_account_locked_until_runs_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    async def exercise() -> None:
        first = AccountRegistry(settings, {}, {})
        second = AccountRegistry(settings, {}, {})
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        try:
            alice = await first.get(ALICE)
            original_close = alice.run_service.close

            async def delayed_close() -> None:
                close_started.set()
                await allow_close.wait()
                await original_close()

            monkeypatch.setattr(alice.run_service, "close", delayed_close)
            close_task = asyncio.create_task(first.close())
            await close_started.wait()
            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task

            with pytest.raises(ApplicationError) as conflict:
                await second.get(ALICE)
            assert conflict.value.code == "account_in_use"

            allow_close.set()
            await first.close()
            recovered = await second.get(ALICE)
            assert (await recovered.profile_service.get()).id == str(ALICE)
        finally:
            allow_close.set()
            await first.close()
            await second.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("foreign_directory", ["accounts", "account"])
def test_registry_rejects_account_directory_owned_by_other_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_directory: str
) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    account_dir = settings.data_dir / "accounts" / str(ALICE)
    settings.data_dir.mkdir(mode=0o700)
    account_dir.parent.mkdir(mode=0o700)
    account_dir.mkdir(mode=0o700)
    target = account_dir.parent if foreign_directory == "accounts" else account_dir
    original_stat = Path.stat

    def foreign_owner_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path != target:
            return result
        values = list(result)
        values[4] = result.st_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", foreign_owner_stat)

    async def exercise() -> None:
        registry = AccountRegistry(settings, {}, {})
        try:
            with pytest.raises(RuntimeError, match="owned"):
                await registry.get(ALICE)
        finally:
            await registry.close()

    asyncio.run(exercise())
