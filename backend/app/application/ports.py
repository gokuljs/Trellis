from collections.abc import AsyncGenerator, Sequence
from typing import Protocol

from app.application.budgets import BudgetPreset
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
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    RunEvent,
    RunEventType,
    RunMessageRecord,
    RunSnapshot,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
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

    async def get_default_budget_preset(self) -> BudgetPreset: ...

    async def set_default_budget_preset(self, preset: BudgetPreset) -> None: ...


class OnboardingRepository(Protocol):
    async def get_onboarding_progress(self) -> OnboardingProgress: ...

    async def advance_onboarding_intro(self) -> OnboardingProgress: ...

    async def save_onboarding_profile(
        self, display_name: str, email: str
    ) -> tuple[UserProfile, OnboardingProgress]: ...

    async def complete_onboarding(self, model_id: ModelId) -> bool: ...


class SessionRepository(Protocol):
    async def create_session(self, workspace_path: str | None = None) -> Session: ...

    async def set_session_workspace(
        self, session_id: str, workspace_path: str | None
    ) -> Session | None: ...

    async def list_sessions(self) -> list[Session]: ...

    async def get_session(self, session_id: str) -> Session | None: ...

    async def list_messages(self, session_id: str) -> list[Message]: ...


class WorkspaceDirectoryPicker(Protocol):
    async def pick_directory(self) -> str | None: ...


class RunRepository(Protocol):
    async def create_run(
        self,
        session_id: str,
        turn_id: str,
        client_request_id: str,
        content: str,
        model: ModelDescriptor,
        *,
        budget_preset: BudgetPreset = "conservative",
    ) -> RunSnapshot: ...

    async def get_run(self, run_id: str) -> RunSnapshot | None: ...

    async def get_latest_run_for_session(self, session_id: str) -> RunSnapshot | None: ...

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

    async def request_run_cancellation(
        self,
        run_id: str,
    ) -> tuple[RunSnapshot, RunEvent | None]: ...

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
        cache_creation_tokens: int | None = None,
        estimated_cost: float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ModelCallRecord: ...

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]: ...

    async def record_assistant_message(
        self,
        run_id: str,
        model_call_id: str,
        message: ModelMessage,
    ) -> tuple[RunMessageRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]: ...

    async def record_tool_result(
        self,
        run_id: str,
        tool_call_id: str,
        content: str,
        *,
        status: ToolCallStatus = ToolCallStatus.COMPLETED,
    ) -> tuple[RunMessageRecord, ToolCallRecord, RunEvent]: ...

    async def record_tool_approval_decision(
        self,
        run_id: str,
        tool_call_id: str,
        decision: ToolApprovalDecision,
    ) -> tuple[ToolCallRecord, RunEvent | None]: ...

    async def request_tool_approval(
        self,
        run_id: str,
        tool_call_id: str,
        preview: dict[str, object] | None,
    ) -> tuple[RunSnapshot, RunEvent]: ...

    async def resume_approved_run(self, run_id: str) -> tuple[RunSnapshot, RunEvent]: ...

    async def list_decided_approval_runs(self) -> list[str]: ...

    async def list_run_messages(self, run_id: str) -> list[RunMessageRecord]: ...

    async def list_tool_calls(self, run_id: str) -> list[ToolCallRecord]: ...


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
