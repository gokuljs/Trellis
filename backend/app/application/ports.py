from collections.abc import Sequence
from typing import Protocol

from app.domain.models import (
    Message,
    ModelDescriptor,
    ModelId,
    OnboardingProgress,
    ProviderName,
    Session,
    UserProfile,
)


class ProfileRepository(Protocol):
    async def get_profile(self) -> UserProfile: ...

    async def update_profile(self, display_name: str | None, email: str | None) -> UserProfile: ...


class SettingsRepository(Protocol):
    async def get_selected_provider(self) -> ProviderName: ...

    async def set_selected_provider(self, provider: ProviderName) -> ProviderName: ...

    async def get_selected_model_id(self) -> ModelId: ...

    async def list_models(self) -> list[ModelDescriptor]: ...

    async def set_selected_model(self, model_id: ModelId) -> bool: ...


class OnboardingRepository(Protocol):
    async def get_onboarding_progress(self) -> OnboardingProgress: ...

    async def advance_onboarding_intro(self) -> OnboardingProgress: ...

    async def save_onboarding_profile(
        self, display_name: str, email: str
    ) -> tuple[UserProfile, OnboardingProgress]: ...

    async def complete_onboarding(self, model_id: ModelId) -> bool: ...


class SessionRepository(Protocol):
    async def create_session(self) -> Session: ...

    async def list_sessions(self) -> list[Session]: ...

    async def get_session(self, session_id: str) -> Session | None: ...

    async def list_messages(self, session_id: str) -> list[Message]: ...


class ChatRepository(ProfileRepository, SettingsRepository, SessionRepository, Protocol):
    async def get_turn_messages(self, session_id: str, turn_id: str) -> list[Message]: ...

    async def claim_turn(self, session_id: str, turn_id: str) -> bool: ...

    async def release_turn(self, session_id: str, turn_id: str) -> None: ...

    async def add_user_message(self, session_id: str, turn_id: str, content: str) -> Message: ...

    async def add_assistant_message(
        self,
        session_id: str,
        turn_id: str,
        content: str,
        provider: ProviderName,
        model: str,
    ) -> Message: ...


class SecretStorePort(Protocol):
    async def get(self, provider: ProviderName) -> str | None: ...

    async def set(self, provider: ProviderName, value: str) -> None: ...

    async def delete(self, provider: ProviderName) -> None: ...

    async def status(self, provider: ProviderName) -> tuple[bool, str | None]: ...


class ProviderAdapter(Protocol):
    name: ProviderName
    model: str

    async def complete(
        self,
        messages: Sequence[Message],
        api_key: str,
        user_id: str,
    ) -> str: ...
