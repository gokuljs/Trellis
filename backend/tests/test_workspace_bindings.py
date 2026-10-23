import asyncio
import stat
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.application.errors import ApplicationError
from app.application.sessions import SessionService
from app.domain.capabilities import RuntimeCapabilities
from app.domain.models import Session
from app.infrastructure import workspace_grants
from app.infrastructure.accounts import _installation_identity, _prepare_account_directory
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from app.infrastructure.workspace_grants import InstallationIdentity, WorkspaceGrantStore


def bindings(account: Path, account_id: str, installation: InstallationIdentity):
    return LocalWorkspaceBindings(WorkspaceGrantStore(account, account_id, installation))


def test_bindings_restore_only_explicit_grants_and_expose_pinned_reference(tmp_path: Path) -> None:
    async def scenario() -> None:
        account = tmp_path / "account"
        account.mkdir(mode=0o700)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        account_id, session_id = str(uuid4()), str(uuid4())
        installation = InstallationIdentity(str(uuid4()), "a" * 64)
        original = bindings(account, account_id, installation)
        assert await original.resolve(session_id, str(workspace)) is None
        await original.bind(session_id, workspace)
        fresh = bindings(account, account_id, installation)
        assert await fresh.resolve(session_id, str(workspace)) == workspace
        reference = await fresh.reference(session_id, str(workspace))
        assert reference is not None
        assert reference.canonical_path == str(workspace)
        assert fresh.installation_id == installation.installation_id
        pinned = await fresh.pin(session_id, str(workspace))
        assert pinned is not None
        with pinned:
            assert pinned.verify()
        assert await fresh.resolve(str(uuid4()), str(workspace)) is None
        await fresh.bind(session_id, None)
        assert await original.resolve(session_id, str(workspace)) is None
        assert await original.reference(session_id, str(workspace)) is None
        assert await original.pin(session_id, str(workspace)) is None

    asyncio.run(scenario())


def test_process_only_bindings_never_trust_cloud_paths_or_supply_recovery_reference(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        current = LocalWorkspaceBindings()
        assert await current.resolve("session", str(tmp_path)) is None
        await current.bind("session", tmp_path)
        assert await current.resolve("session", str(tmp_path)) == tmp_path
        assert await current.reference("session", str(tmp_path)) is None
        assert await current.pin("session", str(tmp_path)) is None
        assert current.installation_id is None
        assert await LocalWorkspaceBindings().resolve("session", str(tmp_path)) is None
        await current.bind("session", None)
        assert await current.resolve("session", str(tmp_path)) is None

    asyncio.run(scenario())


def test_corrupt_or_other_account_grants_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        account = tmp_path / "account"
        account.mkdir(mode=0o700)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        account_id, session_id = str(uuid4()), str(uuid4())
        installation = InstallationIdentity(str(uuid4()), "a" * 64)
        original = bindings(account, account_id, installation)
        await original.bind(session_id, workspace)
        other = bindings(account, str(uuid4()), installation)
        assert await other.resolve(session_id, str(workspace)) is None
        with pytest.raises(ApplicationError, match="securely"):
            await other.bind(session_id, workspace)
        (account / "workspace-grants.json").write_text("invalid")
        assert await original.resolve(session_id, str(workspace)) is None
        with pytest.raises(ApplicationError, match="securely"):
            await original.bind(session_id, None)

    asyncio.run(scenario())


def test_clearing_revokes_permission_before_cloud_update_and_stops_on_storage_failure(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        account = tmp_path / "account"
        account.mkdir(mode=0o700)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        account_id, session_id = str(uuid4()), str(uuid4())
        installation = InstallationIdentity(str(uuid4()), "a" * 64)
        current = bindings(account, account_id, installation)
        await current.bind(session_id, workspace)
        session = Session(session_id, account_id, "", "now", "now", 0, str(workspace))
        repository = AsyncMock()
        service = SessionService(
            repository,
            workspace_bindings=current,
            capabilities=RuntimeCapabilities(frozenset({"local_read"})),
        )

        async def clear_cloud(_session_id: str, path: str | None) -> Session:
            assert path is None
            assert await current.resolve(session_id, str(workspace)) is None
            return replace(session, workspace_path=None)

        repository.set_session_workspace.side_effect = clear_cloud
        assert (await service.set_workspace(session_id, None)).workspace_path is None
        await current.bind(session_id, workspace)
        (account / "workspace-grants.json").write_text("invalid")
        repository.set_session_workspace.reset_mock()
        with pytest.raises(ApplicationError):
            await service.set_workspace(session_id, None)
        repository.set_session_workspace.assert_not_awaited()

    asyncio.run(scenario())


def test_creation_and_folder_selection_await_durable_grant(tmp_path: Path) -> None:
    async def scenario() -> None:
        account = tmp_path / "account"
        account.mkdir(mode=0o700)
        account_id, session_id = str(uuid4()), str(uuid4())
        installation = InstallationIdentity(str(uuid4()), "a" * 64)
        current = bindings(account, account_id, installation)
        session = Session(session_id, account_id, "", "now", "now", 0, str(tmp_path))
        repository = AsyncMock()
        repository.create_session.return_value = session
        repository.set_session_workspace.return_value = session
        service = SessionService(
            repository,
            workspace_bindings=current,
            capabilities=RuntimeCapabilities(frozenset({"local_read"})),
        )
        await service.create(str(tmp_path))
        fresh = bindings(account, account_id, installation)
        assert await fresh.resolve(session_id, str(tmp_path)) == tmp_path
        original_reference = await fresh.reference(session_id, str(tmp_path))
        await service.set_workspace(session_id, str(tmp_path))
        assert await fresh.reference(session_id, str(tmp_path)) != original_reference
        assert await service.workspace_ready(session)

    asyncio.run(scenario())


def test_replaced_folder_invalidates_both_remembered_and_process_only_binding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        account = tmp_path / "account"
        account.mkdir(mode=0o700)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        installation = InstallationIdentity(str(uuid4()), "a" * 64)
        current = bindings(account, str(uuid4()), installation)
        process_only = LocalWorkspaceBindings()
        session_id = str(uuid4())
        await current.bind(session_id, workspace)
        await process_only.bind(session_id, workspace)
        workspace.rename(tmp_path / "old-workspace")
        workspace.mkdir()
        assert await current.resolve(session_id, str(workspace)) is None
        assert await process_only.resolve(session_id, str(workspace)) is None
        assert await current.reference(session_id, str(workspace)) is None

    asyncio.run(scenario())


def test_cancelled_grant_write_finishes_before_a_following_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        account = tmp_path / "account"
        account.mkdir(mode=0o700)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        account_id, session_id = str(uuid4()), str(uuid4())
        installation = InstallationIdentity(str(uuid4()), "a" * 64)
        current = bindings(account, account_id, installation)
        await current.bind(session_id, workspace)
        write_entered = threading.Event()
        write_allowed = threading.Event()
        original_save = workspace_grants._save

        def delayed_save(directory_fd, name, value):
            if value["grants"] == []:
                write_entered.set()
                assert write_allowed.wait(timeout=3)
            return original_save(directory_fd, name, value)

        monkeypatch.setattr(workspace_grants, "_save", delayed_save)
        revocation = asyncio.create_task(current.bind(session_id, None))
        try:
            assert await asyncio.to_thread(write_entered.wait, 2)
            revocation.cancel()
            await asyncio.sleep(0)
            assert not revocation.done()
            replacement = asyncio.create_task(current.bind(session_id, workspace))
            await asyncio.sleep(0)
            assert not replacement.done()
        finally:
            write_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await revocation
        await replacement
        fresh = bindings(account, account_id, installation)
        assert await fresh.resolve(session_id, str(workspace)) == workspace

    asyncio.run(scenario())


def test_installation_storage_is_private_and_canonical_and_rejects_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_grants, "_computer_identity", lambda: "a" * 64)
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    first = _installation_identity(data)
    assert _installation_identity(data) == first
    assert stat.S_IMODE((data / "installation").stat().st_mode) == 0o700
    user_id = uuid4()
    account = _prepare_account_directory(data / ".." / "data", user_id)
    assert account == (data / "accounts" / str(user_id)).resolve(strict=True)
    assert _installation_identity(data / ".." / "data") == first
    (data / "installation").rename(data / "original-installation")
    (data / "installation").symlink_to(data / "original-installation", target_is_directory=True)
    with pytest.raises(ApplicationError, match="securely"):
        _installation_identity(data)
