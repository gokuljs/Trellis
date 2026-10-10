import asyncio
from pathlib import Path

from app.application.errors import ApplicationError
from app.application.ports import SessionRepository, WorkspaceBindingPort
from app.domain.models import Session, SessionDetail, SessionWorkspaceBusy


def _resolved_workspace(raw_path: str) -> str:
    supplied = Path(raw_path)
    if not supplied.is_absolute():
        raise ApplicationError("invalid_workspace", "Enter an absolute folder path.")
    try:
        resolved = supplied.resolve(strict=True)
        if not resolved.is_dir():
            raise OSError("Not a directory")
    except OSError, RuntimeError, ValueError:
        raise ApplicationError(
            "invalid_workspace", "Choose an existing folder that Trellis can access."
        ) from None
    return str(resolved)


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        *,
        workspace_bindings: WorkspaceBindingPort | None = None,
    ) -> None:
        self._repository = repository
        self._workspace_bindings = workspace_bindings

    async def list_sessions(self) -> list[Session]:
        return await self._repository.list_sessions()

    async def create(self, workspace_path: str | None = None) -> Session:
        resolved = (
            await asyncio.to_thread(_resolved_workspace, workspace_path)
            if workspace_path is not None
            else None
        )
        session = await self._repository.create_session(resolved)
        if self._workspace_bindings is not None:
            self._workspace_bindings.bind(
                session.id, Path(resolved) if resolved is not None else None
            )
        return session

    async def set_workspace(self, session_id: str, workspace_path: str | None) -> Session:
        resolved = (
            await asyncio.to_thread(_resolved_workspace, workspace_path)
            if workspace_path is not None
            else None
        )
        try:
            session = await self._repository.set_session_workspace(session_id, resolved)
        except SessionWorkspaceBusy:
            raise ApplicationError(
                "session_workspace_busy", "Wait for this run to finish before changing its folder."
            ) from None
        if session is None:
            raise ApplicationError("session_not_found", "Session not found.")
        if self._workspace_bindings is not None:
            self._workspace_bindings.bind(
                session.id, Path(resolved) if resolved is not None else None
            )
        return session

    async def get(self, session_id: str) -> SessionDetail | None:
        session = await self._repository.get_session(session_id)
        if session is None:
            return None
        return SessionDetail(
            session=session,
            messages=await self._repository.list_messages(session_id),
        )
