"""Resolve explicit folder permissions without trusting cloud paths alone."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from app.application.errors import ApplicationError
from app.domain.recovery import WorkspaceRecoveryReference
from app.infrastructure.workspace_grants import PinnedWorkspace, WorkspaceGrantStore


def _directory_identity(workspace_root: Path) -> tuple[int, int] | None:
    try:
        if (
            not workspace_root.is_absolute()
            or workspace_root.resolve(strict=True) != workspace_root
            or not workspace_root.is_dir()
        ):
            return None
        metadata = workspace_root.stat()
        return metadata.st_dev, metadata.st_ino
    except OSError, RuntimeError, ValueError:
        return None


async def _off_loop[T](
    operation: Callable[[], T], *, close_cancelled: Callable[[T], None] | None = None
) -> T:
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancelled = error
    if cancelled is not None:
        if close_cancelled is not None:
            close_cancelled(result)
        raise cancelled
    return result


def _close_pin(pinned: PinnedWorkspace | None) -> None:
    if pinned is not None:
        pinned.close()


def _grant_unavailable() -> ApplicationError:
    return ApplicationError(
        "workspace_grant_unavailable",
        "Trellis could not securely update this folder permission. "
        "Check local storage and try again.",
    )


class LocalWorkspaceBindings:
    def __init__(self, store: WorkspaceGrantStore | None = None) -> None:
        self._store = store
        self._selected: dict[str, tuple[Path, tuple[int, int]]] = {}
        self._selection_lock = asyncio.Lock()

    @property
    def installation_id(self) -> str | None:
        return self._store.installation.installation_id if self._store is not None else None

    async def bind(self, session_id: str, workspace_root: Path | None) -> None:
        async with self._selection_lock:
            store = self._store
            if store is not None:
                try:
                    await _off_loop(lambda: store.select(session_id, workspace_root))
                except OSError, RuntimeError, ValueError:
                    raise _grant_unavailable() from None
            elif workspace_root is None:
                self._selected.pop(session_id, None)
            else:
                identity = await _off_loop(lambda: _directory_identity(workspace_root))
                if identity is None:
                    raise _grant_unavailable()
                self._selected[session_id] = workspace_root, identity

    async def resolve(self, session_id: str, workspace_path: str | None) -> Path | None:
        store = self._store
        if store is not None:
            grant = await _off_loop(lambda: store.resolve(session_id, workspace_path))
            return Path(grant.canonical_path) if grant is not None else None
        selected = self._selected.get(session_id)
        if selected is None or workspace_path != str(selected[0]):
            return None
        path, identity = selected
        if await _off_loop(lambda: _directory_identity(path)) != identity:
            self._selected.pop(session_id, None)
            return None
        return path

    async def reference(
        self, session_id: str, workspace_path: str | None
    ) -> WorkspaceRecoveryReference | None:
        store = self._store
        if store is None:
            return None
        grant = await _off_loop(lambda: store.resolve(session_id, workspace_path))
        return grant.reference() if grant is not None else None

    async def pin(self, session_id: str, workspace_path: str | None) -> PinnedWorkspace | None:
        if self._store is None:
            return None

        def open_pin() -> PinnedWorkspace | None:
            assert self._store is not None
            grant = self._store.resolve(session_id, workspace_path)
            return self._store.pin(grant) if grant is not None else None

        try:
            return await _off_loop(open_pin, close_cancelled=_close_pin)
        except OSError, RuntimeError, ValueError:
            raise _grant_unavailable() from None
