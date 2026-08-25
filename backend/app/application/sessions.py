from app.application.ports import SessionRepository
from app.domain.models import Session, SessionDetail


class SessionService:
    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository

    async def list_sessions(self) -> list[Session]:
        return await self._repository.list_sessions()

    async def create(self) -> Session:
        return await self._repository.create_session()

    async def get(self, session_id: str) -> SessionDetail | None:
        session = await self._repository.get_session(session_id)
        if session is None:
            return None
        return SessionDetail(
            session=session,
            messages=await self._repository.list_messages(session_id),
        )
