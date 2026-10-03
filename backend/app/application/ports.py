from collections.abc import AsyncGenerator, Sequence
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
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelRequest,
    ModelStreamEvent,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
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


class RunRepository(Protocol):
    async def create_run(
        self,
        session_id: str,
        turn_id: str,
        client_request_id: str,
        content: str,
        model: ModelDescriptor,
    ) -> RunSnapshot: ...

    async def get_run(self, run_id: str) -> RunSnapshot | None: ...

    async def append_run_event(
        self,
        run_id: str,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        event_version: int = 1,
    ) -> RunEvent: ...

    async def list_run_events(
        self,
        run_id: str,
        after_sequence: int = 0,
        *,
        limit: int = 500,
    ) -> list[RunEvent]: ...

    async def transition_run_record(
        self,
        run_id: str,
        next_status: RunStatus,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        stop_reason: str | None = None,
    ) -> RunEvent: ...

    async def complete_run(
        self,
        run_id: str,
        content: str,
    ) -> tuple[Message, tuple[RunEvent, ...]]: ...

    async def create_model_call(
        self,
        run_id: str,
        step_index: int,
        model: ModelDescriptor,
        request_snapshot: dict[str, object],
    ) -> ModelCallRecord: ...

    async def update_model_call(
        self,
        call_id: str,
        next_status: ModelCallStatus,
        *,
        response_snapshot: dict[str, object] | None = None,
        provider_response_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cached_tokens: int | None = None,
        estimated_cost: float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ModelCallRecord: ...

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]: ...


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


class StreamingProviderAdapter(ProviderAdapter, Protocol):
    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]: ...


class RunEventPublisher(Protocol):
    async def publish(self, event: RunEvent) -> None: ...
