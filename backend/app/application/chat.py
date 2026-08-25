import asyncio
from collections.abc import Mapping, Sequence
from typing import Protocol

from app.application.errors import ApplicationError
from app.domain.models import Message, ProviderName, Session, TurnResult
from app.infrastructure.database import Database
from app.infrastructure.providers import ProviderError
from app.infrastructure.secrets import SecretStore


class ProviderAdapter(Protocol):
    name: ProviderName
    model: str

    async def complete(
        self,
        messages: Sequence[Message],
        api_key: str,
        user_id: str,
    ) -> str: ...


class ChatService:
    def __init__(
        self,
        database: Database,
        secret_store: SecretStore,
        providers: Mapping[ProviderName, ProviderAdapter],
    ) -> None:
        self._database = database
        self._secret_store = secret_store
        self._providers = providers
        self._active_sessions: set[str] = set()
        self._active_sessions_lock = asyncio.Lock()

    async def complete_turn(self, session_id: str, turn_id: str, content: str) -> TurnResult:
        session = await self._database.get_session(session_id)
        if session is None:
            raise ApplicationError("session_not_found", "Session not found")

        normalized_content = content.strip()
        existing = await self._database.get_turn_messages(session_id, turn_id)
        completed = self._resolve_existing(existing, normalized_content)
        if completed is not None:
            return TurnResult(session=session, **completed)

        await self._claim_session(session_id)
        try:
            existing = await self._database.get_turn_messages(session_id, turn_id)
            completed = self._resolve_existing(existing, normalized_content)
            if completed is not None:
                refreshed = await self._require_session(session_id)
                return TurnResult(session=refreshed, **completed)

            provider_name = await self._database.get_selected_provider()
            api_key = await self._secret_store.get(provider_name)
            if api_key is None:
                raise ApplicationError(
                    "provider_not_configured",
                    "Add an API key for the selected provider in Settings.",
                )
            provider = self._providers.get(provider_name)
            if provider is None:
                raise ApplicationError(
                    "provider_not_available",
                    "The selected provider is not available.",
                )

            if existing:
                user_message = existing[0]
            else:
                user_message = await self._database.add_user_message(
                    session_id,
                    turn_id,
                    normalized_content,
                )

            history = await self._database.list_messages(session_id)
            profile = await self._database.get_profile()
            try:
                assistant_content = (await provider.complete(history, api_key, profile.id)).strip()
            except ProviderError as error:
                raise ApplicationError(error.code, error.message) from None
            if not assistant_content:
                raise ApplicationError(
                    "provider_invalid_response",
                    "The provider returned an empty response.",
                )
            assistant_message = await self._database.add_assistant_message(
                session_id,
                turn_id,
                assistant_content,
                provider.name,
                provider.model,
            )
            refreshed = await self._require_session(session_id)
            return TurnResult(
                session=refreshed,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        finally:
            await self._release_session(session_id)

    @staticmethod
    def _resolve_existing(messages: Sequence[Message], content: str) -> dict[str, Message] | None:
        if not messages:
            return None
        user_message = messages[0]
        if user_message.content != content:
            raise ApplicationError(
                "turn_conflict",
                "This turn ID is already associated with different content.",
            )
        if len(messages) == 2:
            return {"user_message": user_message, "assistant_message": messages[1]}
        return None

    async def _claim_session(self, session_id: str) -> None:
        async with self._active_sessions_lock:
            if session_id in self._active_sessions:
                raise ApplicationError(
                    "turn_in_progress",
                    "Another turn is already in progress for this session.",
                )
            self._active_sessions.add(session_id)

    async def _release_session(self, session_id: str) -> None:
        async with self._active_sessions_lock:
            self._active_sessions.discard(session_id)

    async def _require_session(self, session_id: str) -> Session:
        session = await self._database.get_session(session_id)
        if session is None:
            raise ApplicationError("session_not_found", "Session not found")
        return session
