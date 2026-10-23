"""Local evidence of explicit folder selection, bound to this installation."""

import errno
import fcntl
import hashlib
import json
import os
import plistlib
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from app.domain.recovery import WorkspaceRecoveryReference

MAX_GRANTS = 1000
MAX_STORAGE_BYTES = 512 * 1024
STORAGE_VERSION = 1


class WorkspaceGrantError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _uuid(value: object) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid identity")
    return value


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("Invalid digest")
    return value


def _number(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**64:
        raise ValueError("Invalid directory identity")
    return value


def _computer_identity() -> str:
    if sys.platform == "darwin":
        process = subprocess.Popen(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice", "-a"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdout is not None
            raw, _ = process.communicate(timeout=2)
            if process.returncode != 0 or len(raw) > 64 * 1024:
                raise ValueError("Computer identity unavailable")
            records = plistlib.loads(raw)
            identity = records[0]["IOPlatformUUID"]
            if not isinstance(identity, str):
                raise ValueError("Computer identity unavailable")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
    elif sys.platform.startswith("linux"):
        with Path("/etc/machine-id").open("rb") as source:
            raw = source.read(257)
        if not raw.strip() or len(raw) > 256:
            raise ValueError("Computer identity unavailable")
        identity = raw.decode("ascii").strip()
    else:
        raise WorkspaceGrantError("computer_identity_unavailable")
    return hashlib.sha256(f"{sys.platform}:{identity}".encode()).hexdigest()


@contextmanager
def _private_directory(path: Path) -> Iterator[int]:
    descriptor: int | None = None
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise WorkspaceGrantError("unsafe_storage")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise WorkspaceGrantError("unsafe_storage")
        yield descriptor
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, WorkspaceGrantError):
            raise
        raise WorkspaceGrantError("unsafe_storage") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_file(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_STORAGE_BYTES
    ):
        raise WorkspaceGrantError("unsafe_storage")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _read(directory_fd: int, name: str) -> dict[str, object] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    try:
        _private_file(descriptor)
        data = os.read(descriptor, MAX_STORAGE_BYTES + 1)
        if len(data) > MAX_STORAGE_BYTES:
            raise WorkspaceGrantError("unsafe_storage")
        value: object = json.loads(data, object_pairs_hook=_object)
        if not isinstance(value, dict):
            raise WorkspaceGrantError("corrupt_storage")
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise WorkspaceGrantError("corrupt_storage") from error
    finally:
        os.close(descriptor)


def _save(directory_fd: int, name: str, value: dict[str, object]) -> None:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > MAX_STORAGE_BYTES:
        raise WorkspaceGrantError("storage_limit")
    temporary = f".{name}.{secrets.token_hex(12)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("Incomplete grant write")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)


def _flock(descriptor: int, mode: int, timeout_seconds: float, code: str) -> None:
    deadline = time.monotonic() + max(0, min(timeout_seconds, 5))
    while True:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkspaceGrantError(code) from None
            time.sleep(min(0.01, remaining))


@contextmanager
def _storage_lock(directory_fd: int, name: str) -> Iterator[None]:
    descriptor = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        _private_file(descriptor)
        _flock(descriptor, fcntl.LOCK_EX, 2, "storage_busy")
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class InstallationIdentity:
    installation_id: str
    computer_id: str

    @classmethod
    def load(cls, data_dir: Path, *, computer_id: str | None = None) -> Self:
        try:
            identity = _digest(computer_id if computer_id is not None else _computer_identity())
        except (OSError, ValueError, subprocess.SubprocessError, KeyError, IndexError) as error:
            raise WorkspaceGrantError("computer_identity_unavailable") from error
        with (
            _private_directory(data_dir) as directory_fd,
            _storage_lock(directory_fd, ".installation.lock"),
        ):
            saved = _read(directory_fd, "installation.json")
            if saved is None:
                installation_id = str(uuid4())
                _save(
                    directory_fd,
                    "installation.json",
                    {
                        "version": STORAGE_VERSION,
                        "installation_id": installation_id,
                        "computer_id": identity,
                    },
                )
            else:
                try:
                    installation_id = _uuid(saved.get("installation_id"))
                    if saved.get("version") != STORAGE_VERSION:
                        raise ValueError("Incompatible installation")
                    if _digest(saved.get("computer_id")) != identity:
                        raise WorkspaceGrantError("installation_changed")
                except ValueError as error:
                    raise WorkspaceGrantError("corrupt_storage") from error
        return cls(installation_id, identity)


@dataclass(frozen=True, slots=True)
class WorkspaceGrant:
    grant_id: str
    session_id: str
    canonical_path: str
    device: int
    inode: int
    revision: int

    def reference(self) -> WorkspaceRecoveryReference:
        return WorkspaceRecoveryReference(
            grant_id=self.grant_id,
            grant_revision=self.revision,
            canonical_path=self.canonical_path,
            directory_device=self.device,
            directory_inode=self.inode,
        )


def _parse_grant(raw: object) -> WorkspaceGrant:
    if not isinstance(raw, dict):
        raise ValueError("Invalid workspace grant")
    path = raw.get("canonical_path")
    device, inode, revision = [_number(raw.get(field)) for field in ("device", "inode", "revision")]
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or len(path.encode("utf-8")) > 4096
        or revision == 0
    ):
        raise ValueError("Invalid workspace grant")
    return WorkspaceGrant(
        _uuid(raw.get("grant_id")),
        _uuid(raw.get("session_id")),
        path,
        device,
        inode,
        revision,
    )


def _open_workspace(path: Path, grant: WorkspaceGrant | None = None) -> int:
    descriptor: int | None = None
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise WorkspaceGrantError("workspace_changed")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        metadata = os.fstat(descriptor)
        if grant is not None and (metadata.st_dev, metadata.st_ino) != (grant.device, grant.inode):
            raise WorkspaceGrantError("workspace_changed")
        return descriptor
    except (OSError, RuntimeError, ValueError) as error:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(error, WorkspaceGrantError):
            raise
        raise WorkspaceGrantError("workspace_changed") from error


class WorkspaceGrantStore:
    def __init__(
        self, account_dir: Path, account_id: str, installation: InstallationIdentity
    ) -> None:
        self._account_dir = account_dir
        self.account_id = _uuid(account_id)
        self.installation = installation

    def _load(self, directory_fd: int) -> dict[str, WorkspaceGrant]:
        saved = _read(directory_fd, "workspace-grants.json")
        if saved is None:
            return {}
        if (
            saved.get("account_id") != self.account_id
            or saved.get("installation_id") != self.installation.installation_id
            or saved.get("computer_id") != self.installation.computer_id
        ):
            raise WorkspaceGrantError("identity_mismatch")
        try:
            if saved.get("version") != STORAGE_VERSION:
                raise ValueError("Incompatible grants")
            entries = saved.get("grants")
            if not isinstance(entries, list) or len(entries) > MAX_GRANTS:
                raise ValueError("Invalid grants")
            grants = [_parse_grant(entry) for entry in entries]
            result = {grant.session_id: grant for grant in grants}
            if len(result) != len(grants):
                raise ValueError("Duplicate session grant")
            return result
        except (ValueError, TypeError) as error:
            raise WorkspaceGrantError("corrupt_storage") from error

    def select(self, session_id: str, workspace_root: Path | None) -> WorkspaceGrant | None:
        session_id = _uuid(session_id)
        with (
            _private_directory(self._account_dir) as directory_fd,
            _storage_lock(directory_fd, ".workspace-grants.lock"),
        ):
            grants = self._load(directory_fd)
            previous = grants.get(session_id)
            if workspace_root is None:
                grants.pop(session_id, None)
                grant = None
            else:
                descriptor = _open_workspace(workspace_root)
                try:
                    metadata = os.fstat(descriptor)
                    grant = WorkspaceGrant(
                        str(uuid4()),
                        session_id,
                        str(workspace_root),
                        metadata.st_dev,
                        metadata.st_ino,
                        previous.revision + 1 if previous is not None else 1,
                    )
                finally:
                    os.close(descriptor)
                grants[session_id] = grant
            if len(grants) > MAX_GRANTS:
                raise WorkspaceGrantError("storage_limit")
            _save(
                directory_fd,
                "workspace-grants.json",
                {
                    "version": STORAGE_VERSION,
                    "account_id": self.account_id,
                    "installation_id": self.installation.installation_id,
                    "computer_id": self.installation.computer_id,
                    "grants": [asdict(entry) for entry in grants.values()],
                },
            )
            return grant

    def resolve(self, session_id: str, workspace_path: str | None) -> WorkspaceGrant | None:
        if workspace_path is None:
            return None
        try:
            with _private_directory(self._account_dir) as directory_fd:
                grant = self._load(directory_fd).get(session_id)
            if grant is None or workspace_path != grant.canonical_path:
                return None
            os.close(_open_workspace(Path(workspace_path), grant))
            return grant
        except OSError, ValueError, RuntimeError:
            return None

    def pin(self, grant: WorkspaceGrant) -> PinnedWorkspace:
        if self.resolve(grant.session_id, grant.canonical_path) != grant:
            raise WorkspaceGrantError("workspace_changed")
        return PinnedWorkspace(self, grant, _open_workspace(Path(grant.canonical_path), grant))


class PinnedWorkspace:
    def __init__(self, store: WorkspaceGrantStore, grant: WorkspaceGrant, descriptor: int) -> None:
        self._store = store
        self.grant = grant
        self.descriptor = descriptor
        self.path = Path(grant.canonical_path)
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            os.close(self.descriptor)
            self._closed = True

    def verify(self) -> bool:
        if self._closed:
            return False
        metadata = os.fstat(self.descriptor)
        return (metadata.st_dev, metadata.st_ino) == (
            self.grant.device,
            self.grant.inode,
        ) and self._store.resolve(self.grant.session_id, str(self.path)) == self.grant

    @contextmanager
    def lock(self, *, exclusive: bool = True, timeout_seconds: float = 2) -> Iterator[Self]:
        if not self.verify():
            raise WorkspaceGrantError("workspace_changed")
        lock_fd = os.open(
            ".",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self.descriptor,
        )
        try:
            _flock(
                lock_fd,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                timeout_seconds,
                "workspace_busy",
            )
            try:
                if not self.verify():
                    raise WorkspaceGrantError("workspace_changed")
                yield self
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
