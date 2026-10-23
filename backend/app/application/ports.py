from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from app.application.budgets import BudgetPreset
from app.domain.context import (
    ContextCheckpoint,
    ContextCheckpointDraft,
    ContextWindow,
    InputTokenCount,
    NativeCompactionResult,
)
from app.domain.history import HistoryPage, PageCursor, SessionPage
from app.domain.models import (
    Message,
    ModelDescriptor,
    ModelId,
    OnboardingProgress,
    ProviderName,
    Session,
    UserProfile,
)
from app.domain.outputs import AssistantOutput
from app.domain.recovery import RecoveryResolution, RecoverySnapshot, WorkspaceRecoveryReference
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
    RunSummary,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
    ToolResult,
)
from app.domain.workspaces import PatchReconciliation, WorkspaceReviewBlock


class ProfileRepository(Protocol):
    async def get_profile(self) -> UserProfile: ...

    async def update_profile(self, display_name: str | None, email: str | None) -> UserProfile: ...


class SettingsRepository(Protocol):
    async def get_selected_provider(self) -> ProviderName: ...

    async def set_selected_provider(self, provider: ProviderName) -> ProviderName: ...

    async def get_selected_model_id(self) -> ModelId: ...

    async def list_models(self) -> list[ModelDescriptor]: ...

    async def list_catalog_models(self) -> list[ModelDescriptor]: ...

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
    async def read_session_page(
        self, before: PageCursor | None, *, limit: int = 40
    ) -> SessionPage: ...

    async def read_history_page(
        self, session_id: str, before: PageCursor | None, *, limit: int = 40
    ) -> HistoryPage: ...

    async def get_message(self, session_id: str, message_id: str) -> Message | None: ...

    async def set_session_model(self, session_id: str, model_id: ModelId) -> Session | None: ...

    async def create_session(self, workspace_path: str | None = None) -> Session: ...

    async def set_session_workspace(
        self, session_id: str, workspace_path: str | None
    ) -> Session | None: ...

    async def list_sessions(self) -> list[Session]: ...

    async def get_session(self, session_id: str) -> Session | None: ...

    async def list_messages(self, session_id: str) -> list[Message]: ...

    async def list_assistant_outputs(self, session_id: str) -> list[AssistantOutput]: ...


class WorkspaceDirectoryPicker(Protocol):
    async def pick_directory(self) -> str | None: ...


class WorkspaceBindingPort(Protocol):
    async def bind(self, session_id: str, workspace_root: Path | None) -> None: ...

    async def resolve(self, session_id: str, workspace_path: str | None) -> Path | None: ...

    async def reference(
        self, session_id: str, workspace_path: str | None
    ) -> WorkspaceRecoveryReference | None: ...


class WorkspaceOperationPort(Protocol):
    async def reconcile_patch(self) -> PatchReconciliation: ...

    async def capture_inputs(self) -> dict[str, object]: ...

    async def mark_dispatch(self, dispatch_id: str) -> str: ...

    async def capture_result(self, result: ToolResult) -> dict[str, object]: ...

    async def confirm_saved(self) -> None: ...

    async def verify(self) -> None: ...


class WorkspaceExecutionPort(Protocol):
    def guard(
        self, session_id: str, root: Path, *, exclusive: bool = False
    ) -> AbstractAsyncContextManager[None]: ...

    def operation(
        self,
        run: RunSnapshot,
        tool: ToolCallRecord,
        root: Path,
        prior_tools: Sequence[ToolCallRecord] = (),
    ) -> AbstractAsyncContextManager[WorkspaceOperationPort]: ...

    async def reconcile_patch(
        self, run: RunSnapshot, tool: ToolCallRecord, root: Path
    ) -> PatchReconciliation: ...

    async def validate_observations(
        self, run: RunSnapshot, tools: Sequence[ToolCallRecord], root: Path
    ) -> None: ...

    async def acknowledge_review(self, session_id: str, root: Path, tool_call_id: str) -> None: ...

    async def review_pending(self, session_id: str, root: Path, tool_call_id: str) -> bool: ...

    async def pending_review(self, session_id: str, root: Path) -> WorkspaceReviewBlock | None: ...

    async def reconcile_command_marker(
        self, run: RunSnapshot, tool: ToolCallRecord, root: Path, *, session_id: str | None = None
    ) -> bool: ...


class RunLeasePort(Protocol):
    def owns_run_lease(self, run_id: str) -> bool: ...

    async def renew_run_lease(self, run_id: str) -> RunSnapshot: ...

    async def recover_expired_run(self, run_id: str) -> RunSnapshot: ...


class RecoveryRepository(Protocol):
    async def list_recovery_candidates(self, installation_id: str) -> list[RunSnapshot]: ...

    async def claim_recovery(
        self, run: RunSnapshot, installation_id: str
    ) -> tuple[RunSnapshot, str | None]: ...

    async def read_recovery_snapshot(self, run_id: str) -> RecoverySnapshot: ...

    async def relinquish_run(self, run_id: str) -> RunSnapshot: ...

    async def finish_recovery(self, run_id: str) -> tuple[RunSnapshot, RunEvent]: ...

    async def stop_recovery(
        self, run_id: str, resolution: RecoveryResolution
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]: ...


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
        request_fingerprint: str | None = None,
        submitted_model_id: str | None = None,
        submitted_budget_preset: BudgetPreset | None = None,
    ) -> RunSnapshot: ...

    async def match_run_request(
        self,
        session_id: str,
        client_request_id: str,
        *,
        turn_id: str,
        content: str,
        request_fingerprint: str,
        submitted_model_id: str | None,
        submitted_budget_preset: BudgetPreset | None,
    ) -> RunSnapshot | None: ...

    async def lookup_run(self, session_id: str, client_request_id: str) -> RunSnapshot | None: ...

    async def get_run(self, run_id: str) -> RunSnapshot | None: ...

    async def get_latest_run_for_session(self, session_id: str) -> RunSnapshot | None: ...

    async def list_run_summaries_for_session(
        self,
        session_id: str,
        offset: int,
        limit: int,
        *,
        turn_id: str | None = None,
        newest_first: bool = False,
    ) -> list[RunSummary]: ...

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

    async def commit_generation(
        self,
        run_id: str,
        call_id: str,
        completion: dict[str, object],
        *,
        assistant_message: ModelMessage | None,
        final_content: str | None,
    ) -> tuple[ModelCallRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]: ...

    async def commit_native_compaction(
        self,
        run_id: str,
        call_id: str,
        completion: dict[str, object],
        *,
        status: ModelCallStatus,
    ) -> tuple[ModelCallRecord, tuple[RunEvent, ...]]: ...

    async def append_assistant_batch(
        self, run_id: str, model_call_id: str, batch_index: int, text: str
    ) -> tuple[AssistantOutput, RunEvent]: ...

    async def finish_assistant_output(
        self, run_id: str, model_call_id: str, *, stop_reason: str = "tool_continuation"
    ) -> AssistantOutput | None: ...

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
        clear_estimated_cost: bool = False,
        accounted_total_tokens: int | None = None,
        clear_accounted_total_tokens: bool = False,
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
        result_source: str = "executor",
        result_snapshot: dict[str, object] | None = None,
    ) -> tuple[RunMessageRecord, ToolCallRecord, RunEvent]: ...

    async def begin_tool_execution(
        self,
        run_id: str,
        tool_call_id: str,
        *,
        dispatch_id: str,
        expected_attempt: int,
        recovery_policy: str,
        input_snapshot: dict[str, object] | None = None,
    ) -> tuple[ToolCallRecord, RunEvent]: ...

    async def mark_tool_outcome_unknown(
        self, run_id: str, tool_call_id: str, reason: str
    ) -> ToolCallRecord: ...

    async def acknowledge_workspace_review(
        self, session_id: str, run_id: str, tool_call_id: str
    ) -> ToolCallRecord: ...

    async def authorize_tool_retry(
        self,
        run_id: str,
        tool_call_id: str,
        *,
        expected_dispatch_id: str,
        expected_attempt: int,
        reconciliation: str,
        proof: dict[str, object] | None = None,
    ) -> ToolCallRecord: ...

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
    """Read-only, account-bound provider credential access."""

    async def get(self, provider: ProviderName) -> str | None: ...

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


class ContextCheckpointRepository(Protocol):
    async def load_context_window(self, session_id: str, *, limit: int = 128) -> ContextWindow: ...

    async def load_context_checkpoint(self, session_id: str) -> ContextCheckpoint | None: ...

    async def save_context_checkpoint(
        self, run_id: str, expected_revision: int, draft: ContextCheckpointDraft
    ) -> ContextCheckpoint: ...

    async def commit_summary(
        self,
        run_id: str,
        model_call_id: str,
        completion: dict[str, object],
        expected_revision: int,
        draft: ContextCheckpointDraft,
    ) -> tuple[ModelCallRecord, ContextCheckpoint, tuple[RunEvent, ...]]: ...

    async def list_context_messages(
        self, session_id: str, after_ordinal: int, *, limit: int = 128
    ) -> list[Message]: ...


class TokenCountingProvider(Protocol):
    async def count_input(
        self, request: ModelRequest, api_key: str, user_id: str
    ) -> InputTokenCount: ...


class NativeCompactingProvider(Protocol):
    async def compact(
        self, request: ModelRequest, api_key: str, user_id: str
    ) -> NativeCompactionResult: ...
