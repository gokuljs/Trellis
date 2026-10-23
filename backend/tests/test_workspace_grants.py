import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.infrastructure.workspace_grants import (
    InstallationIdentity,
    WorkspaceGrantError,
    WorkspaceGrantStore,
)


@pytest.fixture
def storage(tmp_path: Path) -> tuple[Path, Path, str, InstallationIdentity]:
    data = tmp_path / "private"
    data.mkdir(mode=0o700)
    account_id = str(uuid4())
    account = data / account_id
    account.mkdir(mode=0o700)
    installation = InstallationIdentity.load(data, computer_id="a" * 64)
    return data, account, account_id, installation


def test_explicit_grant_survives_another_store_and_revocation(
    storage: tuple[Path, Path, str, InstallationIdentity], tmp_path: Path
) -> None:
    _, account, account_id, installation = storage
    root = tmp_path / "workspace"
    root.mkdir()
    session = str(uuid4())
    store = WorkspaceGrantStore(account, account_id, installation)
    assert store.resolve(session, str(root)) is None
    grant = store.select(session, root)
    fresh = WorkspaceGrantStore(account, account_id, installation)
    assert fresh.resolve(session, str(root)) == grant
    assert fresh.resolve(str(uuid4()), str(root)) is None
    fresh.select(session, None)
    assert store.resolve(session, str(root)) is None


def test_installation_is_stable_and_rejects_another_computer(
    storage: tuple[Path, Path, str, InstallationIdentity],
) -> None:
    data, _, _, installation = storage
    assert InstallationIdentity.load(data, computer_id="a" * 64) == installation
    with pytest.raises(WorkspaceGrantError, match="installation_changed"):
        InstallationIdentity.load(data, computer_id="b" * 64)


def test_grants_reject_another_account_installation_or_computer(
    storage: tuple[Path, Path, str, InstallationIdentity], tmp_path: Path
) -> None:
    _, account, account_id, installation = storage
    root = tmp_path / "workspace"
    root.mkdir()
    session = str(uuid4())
    WorkspaceGrantStore(account, account_id, installation).select(session, root)
    other_identities = [
        (str(uuid4()), installation),
        (account_id, InstallationIdentity(str(uuid4()), installation.computer_id)),
        (account_id, InstallationIdentity(installation.installation_id, "b" * 64)),
    ]
    for other_account, other_installation in other_identities:
        store = WorkspaceGrantStore(account, other_account, other_installation)
        assert store.resolve(session, str(root)) is None
        with pytest.raises(WorkspaceGrantError, match="identity_mismatch"):
            store.select(session, root)


def test_replaced_directory_invalidates_grant_and_pinned_descriptor(
    storage: tuple[Path, Path, str, InstallationIdentity], tmp_path: Path
) -> None:
    _, account, account_id, installation = storage
    root = tmp_path / "workspace"
    root.mkdir()
    session = str(uuid4())
    store = WorkspaceGrantStore(account, account_id, installation)
    grant = store.select(session, root)
    assert grant is not None
    with store.pin(grant) as pinned:
        assert pinned.verify()
        root.rename(tmp_path / "previous")
        root.mkdir()
        assert not pinned.verify()
        assert store.resolve(session, str(root)) is None
        with pytest.raises(WorkspaceGrantError, match="workspace_changed"), pinned.lock():
            pytest.fail("A replaced workspace must never be locked for execution")


@pytest.mark.parametrize("damage", ["corrupt", "world_readable", "symlink", "hardlink"])
def test_invalid_storage_fails_closed(
    storage: tuple[Path, Path, str, InstallationIdentity], tmp_path: Path, damage: str
) -> None:
    _, account, account_id, installation = storage
    root = tmp_path / "workspace"
    root.mkdir()
    session = str(uuid4())
    store = WorkspaceGrantStore(account, account_id, installation)
    store.select(session, root)
    saved = account / "workspace-grants.json"
    if damage == "corrupt":
        saved.write_text("{broken")
    elif damage == "world_readable":
        saved.chmod(0o644)
    elif damage == "symlink":
        original = account / "original.json"
        saved.rename(original)
        saved.symlink_to(original)
    else:
        os.link(saved, account / "linked.json")
    assert store.resolve(session, str(root)) is None
    with pytest.raises(WorkspaceGrantError):
        store.select(session, root)


def test_workspace_lock_serializes_different_account_grants(
    storage: tuple[Path, Path, str, InstallationIdentity], tmp_path: Path
) -> None:
    data, account, account_id, installation = storage
    other_account = data / str(uuid4())
    other_account.mkdir(mode=0o700)
    root = tmp_path / "workspace"
    root.mkdir()
    store = WorkspaceGrantStore(account, account_id, installation)
    other_store = WorkspaceGrantStore(other_account, other_account.name, installation)
    first = store.select(str(uuid4()), root)
    second = other_store.select(str(uuid4()), root)
    assert first is not None and second is not None
    with store.pin(first) as pinned, other_store.pin(second) as other:
        with (
            pinned.lock(),
            pytest.raises(WorkspaceGrantError, match="workspace_busy"),
            other.lock(timeout_seconds=0),
        ):
            pytest.fail("Two writers must not hold the same workspace")
        with other.lock(timeout_seconds=0):
            assert other.verify()


def test_selection_rejects_symlink_and_grant_changes_invalidate_old_pins(
    storage: tuple[Path, Path, str, InstallationIdentity], tmp_path: Path
) -> None:
    _, account, account_id, installation = storage
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    store = WorkspaceGrantStore(account, account_id, installation)
    session = str(uuid4())
    with pytest.raises(WorkspaceGrantError, match="workspace_changed"):
        store.select(session, alias)
    grant = store.select(session, root)
    assert grant is not None
    with store.pin(grant) as pinned:
        replacement = store.select(session, root)
        assert replacement is not None
        assert replacement.revision == grant.revision + 1
        assert replacement.grant_id != grant.grant_id
        assert not pinned.verify()


def test_fresh_process_restores_grants_and_obeys_workspace_lock(
    storage: tuple[Path, Path, str, InstallationIdentity], tmp_path: Path
) -> None:
    _, account, account_id, installation = storage
    root = tmp_path / "workspace"
    root.mkdir()
    session = str(uuid4())
    store = WorkspaceGrantStore(account, account_id, installation)
    grant = store.select(session, root)
    assert grant is not None
    code = """
import sys
from pathlib import Path
from app.infrastructure import workspace_grants as wg
account, account_id, installation_id, computer_id, session, root = sys.argv[1:]
identity = wg.InstallationIdentity(installation_id, computer_id)
store = wg.WorkspaceGrantStore(Path(account), account_id, identity)
grant = store.resolve(session, root)
assert grant is not None
with store.pin(grant) as pinned:
    try:
        with pinned.lock(timeout_seconds=0):
            print('available')
    except wg.WorkspaceGrantError as error:
        print(error.code)
"""
    command = [
        sys.executable,
        "-c",
        code,
        str(account),
        account_id,
        installation.installation_id,
        installation.computer_id,
        session,
        str(root),
    ]
    with store.pin(grant) as pinned, pinned.lock():
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        assert result.stdout.strip() == "workspace_busy"
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.stdout.strip() == "available"


def test_same_pin_cannot_admit_two_writers(
    storage: tuple[Path, Path, str, InstallationIdentity], tmp_path: Path
) -> None:
    _, account, account_id, installation = storage
    root = tmp_path / "workspace"
    root.mkdir()
    store = WorkspaceGrantStore(account, account_id, installation)
    grant = store.select(str(uuid4()), root)
    assert grant is not None
    with (
        store.pin(grant) as pinned,
        pinned.lock(),
        pytest.raises(WorkspaceGrantError, match="workspace_busy"),
        pinned.lock(timeout_seconds=0),
    ):
        pytest.fail("A shared pin must not admit concurrent writers")


@pytest.mark.parametrize("filename", ["installation.json", "workspace-grants.json"])
def test_special_storage_files_cannot_block_validation(storage, filename: str) -> None:
    data, account, account_id, installation = storage
    target = (data if filename == "installation.json" else account) / filename
    target.unlink(missing_ok=True)
    os.mkfifo(target, 0o600)
    code = """
import sys
from pathlib import Path
from app.infrastructure.workspace_grants import (
    InstallationIdentity, WorkspaceGrantError, WorkspaceGrantStore,
)
data, account, account_id, installation_id, filename = sys.argv[1:]
try:
    if filename == 'installation.json':
        InstallationIdentity.load(Path(data), computer_id='a' * 64)
        raise AssertionError('Special storage file was trusted')
    identity = InstallationIdentity(installation_id, 'a' * 64)
    store = WorkspaceGrantStore(Path(account), account_id, identity)
    assert store.resolve('session', '/workspace') is None
except WorkspaceGrantError:
    pass
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(data),
            str(account),
            account_id,
            installation.installation_id,
            filename,
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        timeout=2,
    )
