"""A small account repository for API and service tests without hosted Supabase.

The state is shared by path so a second application instance can exercise
restart behavior. It never creates a SQLite file or replaces the production
Supabase repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast
from uuid import UUID, uuid4

import httpx

from app.application.budgets import BudgetPreset, limits_for_preset
from app.core.config import Settings
from app.domain.models import (
    Message,
    MessageRole,
    ModelDescriptor,
    ModelId,
    OnboardingProgress,
    OnboardingStep,
    ProviderName,
    Session,
    SessionWorkspaceBusy,
    UserProfile,
)
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelMessage,
    RunEvent,
    RunEventType,
    RunMessageRecord,
    RunSnapshot,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
    is_lifecycle_event,
    is_terminal_run_status,
    transition_model_call,
    validate_run_event_transition,
)
from app.infrastructure.supabase_repository import VerifiedTokenSource


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _id() -> str:
    return str(uuid4())


MODELS = (
    ModelDescriptor(
        "openai:gpt-5.5",
        "openai",
        "OpenAI",
        "openai",
        "gpt-5.5",
        "GPT-5.5",
        True,
        True,
        True,
        True,
    ),
    ModelDescriptor(
        "anthropic:claude-sonnet-5",
        "anthropic",
        "Anthropic",
        "anthropic",
        "claude-sonnet-5",
        "Claude Sonnet 5",
        True,
        True,
        True,
        True,
    ),
)


@dataclass(slots=True)
class MemoryState:
    user_id: str
    profile: UserProfile
    step: OnboardingStep = "intro"
    selected_provider: str = "openai"
    selected_model_id: str = "openai:gpt-5.5"
    default_budget_preset: BudgetPreset = "conservative"
    models: list[ModelDescriptor] = field(default_factory=lambda: list(MODELS))
    sessions: dict[str, Session] = field(default_factory=dict)
    messages: dict[str, list[Message]] = field(default_factory=dict)
    turn_claims: dict[str, str] = field(default_factory=dict)
    runs: dict[str, RunSnapshot] = field(default_factory=dict)
    events: dict[str, list[RunEvent]] = field(default_factory=dict)
    model_calls: dict[str, ModelCallRecord] = field(default_factory=dict)
    run_messages: dict[str, list[RunMessageRecord]] = field(default_factory=dict)
    tool_calls: dict[str, ToolCallRecord] = field(default_factory=dict)
    waiting_tool_call_id: dict[str, str] = field(default_factory=dict)


class MemoryRepository:
    """An in-memory implementation of account ports used only by tests."""

    _states: ClassVar[dict[Path, MemoryState]] = {}

    def __init__(self, path: Path, *, owner_id: UUID | None = None) -> None:
        self.path = path.resolve()
        self._owner_id = str(owner_id) if owner_id is not None else None
        self._owned_leases: set[str] = set()
        state = self._states.get(self.path)
        if state is None:
            now = _now()
            user_id = self._owner_id or _id()
            state = MemoryState(user_id, UserProfile(user_id, None, None, now, now))
            self._states[self.path] = state
        elif self._owner_id is not None and state.user_id != self._owner_id:
            raise RuntimeError("Account repository owner does not match authenticated user")
        self.state = state

    async def initialize(self) -> None:
        return None

    async def ensure_account(self) -> None:
        return None

    async def import_batch(
        self, table: str, rows: list[dict[str, object]], capability: str
    ) -> None:
        del table, rows, capability
        raise RuntimeError("The in-memory repository cannot import legacy account data")

    async def seal_import(self, capability: str) -> None:
        del capability
        raise RuntimeError("The in-memory repository cannot import legacy account data")

    async def get_profile(self) -> UserProfile:
        return self.state.profile

    async def update_profile(self, display_name: str | None, email: str | None) -> UserProfile:
        del email  # Supabase Auth owns sign-in email.
        self.state.profile = replace(
            self.state.profile, display_name=display_name, updated_at=_now()
        )
        return self.state.profile

    async def get_onboarding_progress(self) -> OnboardingProgress:
        return OnboardingProgress(self.state.step, self.state.step == "complete")

    async def advance_onboarding_intro(self) -> OnboardingProgress:
        if self.state.step == "intro":
            self.state.step = "profile"
        return await self.get_onboarding_progress()

    async def save_onboarding_profile(
        self, display_name: str, email: str
    ) -> tuple[UserProfile, OnboardingProgress]:
        profile = await self.update_profile(display_name, email)
        if self.state.step == "profile":
            self.state.step = "model"
        return profile, await self.get_onboarding_progress()

    async def complete_onboarding(self, model_id: ModelId) -> bool:
        model = next((item for item in self.state.models if item.id == model_id), None)
        if model is None or self.state.step not in {"model", "complete"}:
            return False
        self.state.selected_provider = model.provider_id
        self.state.selected_model_id = model.id
        self.state.step = "complete"
        return True

    async def get_selected_provider(self) -> ProviderName:
        return self.state.selected_provider

    async def set_selected_provider(self, provider: ProviderName) -> ProviderName:
        model = next((item for item in self.state.models if item.provider_id == provider), None)
        if model is not None:
            self.state.selected_provider = provider
            self.state.selected_model_id = model.id
        return provider

    async def get_selected_model_id(self) -> ModelId:
        return self.state.selected_model_id

    async def list_models(self) -> list[ModelDescriptor]:
        return list(self.state.models)

    async def set_selected_model(self, model_id: ModelId) -> bool:
        model = next((item for item in self.state.models if item.id == model_id), None)
        if model is None:
            return False
        self.state.selected_provider = model.provider_id
        self.state.selected_model_id = model.id
        return True

    async def get_default_budget_preset(self) -> BudgetPreset:
        return self.state.default_budget_preset

    async def set_default_budget_preset(self, preset: BudgetPreset) -> None:
        if preset not in {"conservative", "longer"}:
            raise ValueError("Unknown run budget")
        self.state.default_budget_preset = preset

    async def create_session(self, workspace_path: str | None = None) -> Session:
        now = _now()
        session = Session(_id(), self.state.user_id, "New session", now, now, 0, workspace_path)
        self.state.sessions[session.id] = session
        self.state.messages[session.id] = []
        return session

    async def set_session_workspace(
        self, session_id: str, workspace_path: str | None
    ) -> Session | None:
        session = self.state.sessions.get(session_id)
        if session is None:
            return None
        if any(
            run.session_id == session_id
            and run.status
            in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.CANCELLING,
                RunStatus.WAITING_FOR_APPROVAL,
            }
            for run in self.state.runs.values()
        ):
            raise SessionWorkspaceBusy
        updated = replace(session, workspace_path=workspace_path, updated_at=_now())
        self.state.sessions[session_id] = updated
        return updated

    async def list_sessions(self) -> list[Session]:
        return sorted(
            self.state.sessions.values(), key=lambda session: session.updated_at, reverse=True
        )

    async def get_session(self, session_id: str) -> Session | None:
        return self.state.sessions.get(session_id)

    async def list_messages(self, session_id: str) -> list[Message]:
        return list(self.state.messages.get(session_id, []))

    async def get_turn_messages(self, session_id: str, turn_id: str) -> list[Message]:
        return [
            message
            for message in self.state.messages.get(session_id, [])
            if message.turn_id == turn_id
        ]

    async def claim_turn(self, session_id: str, turn_id: str) -> bool:
        if session_id in self.state.turn_claims:
            return False
        if any(
            run.session_id == session_id and not is_terminal_run_status(run.status)
            for run in self.state.runs.values()
        ):
            return False
        self.state.turn_claims[session_id] = turn_id
        return True

    async def release_turn(self, session_id: str, turn_id: str) -> None:
        if self.state.turn_claims.get(session_id) == turn_id:
            del self.state.turn_claims[session_id]

    def _add_message(
        self,
        session_id: str,
        turn_id: str,
        role: str,
        content: str,
        provider: str | None,
        model: str | None,
    ) -> Message:
        session = self.state.sessions[session_id]
        items = self.state.messages[session_id]
        now = _now()
        message = Message(
            _id(),
            session_id,
            turn_id,
            len(items) + 1,
            cast(MessageRole, role),
            content,
            provider,
            model,
            now,
        )
        items.append(message)
        title = (
            " ".join(content.split())[:80] or "New session"
            if role == "user" and len(items) == 1
            else session.title
        )
        self.state.sessions[session_id] = replace(
            session, title=title, updated_at=now, message_count=len(items)
        )
        return message

    async def add_user_message(self, session_id: str, turn_id: str, content: str) -> Message:
        return self._add_message(session_id, turn_id, "user", content, None, None)

    async def add_assistant_message(
        self,
        session_id: str,
        turn_id: str,
        content: str,
        provider: ProviderName,
        model: str,
    ) -> Message:
        return self._add_message(session_id, turn_id, "assistant", content, provider, model)

    def _event(
        self,
        run_id: str,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        event_version: int = 1,
    ) -> RunEvent:
        run = self.state.runs[run_id]
        sequence = run.last_event_sequence + 1
        event = RunEvent(run_id, sequence, event_type, event_version, data, _now())
        self.state.events[run_id].append(event)
        self.state.runs[run_id] = replace(run, last_event_sequence=sequence)
        return event

    async def create_run(
        self,
        session_id: str,
        turn_id: str,
        client_request_id: str,
        content: str,
        model: ModelDescriptor,
        *,
        budget_preset: BudgetPreset = "conservative",
    ) -> RunSnapshot:
        normalized = content.strip()
        if not normalized:
            raise ValueError("run input cannot be empty")
        if not client_request_id or len(client_request_id) > 200:
            raise ValueError("client request ID must contain 1 to 200 characters")
        if session_id not in self.state.sessions:
            raise ValueError("session not found")
        matching = [
            run
            for run in self.state.runs.values()
            if run.session_id == session_id and run.client_request_id == client_request_id
        ]
        if matching:
            existing = matching[-1]
            message = next(
                item
                for item in self.state.messages[session_id]
                if item.id == existing.input_message_id
            )
            if (
                existing.turn_id != turn_id
                or message.content != normalized
                or existing.model_id != model.id
                or existing.provider_id != model.provider_id
                or existing.adapter_kind != model.adapter_kind
                or existing.upstream_model_id != model.upstream_model_id
                or existing.budget_preset != budget_preset
            ):
                raise ValueError("run request conflicts with a previous payload")
            return existing
        prior = next(
            (
                run
                for run in reversed(tuple(self.state.runs.values()))
                if run.session_id == session_id and run.turn_id == turn_id
            ),
            None,
        )
        retry_of = None
        if prior is not None:
            prior_input = next(
                item
                for item in self.state.messages[session_id]
                if item.id == prior.input_message_id
            )
            if prior_input.content != normalized:
                raise ValueError("run request conflicts with a previous payload")
            if prior.status in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.CANCELLING,
                RunStatus.COMPLETED,
            }:
                return prior
            retry_of = prior.id
        if any(
            run.session_id == session_id
            and run.status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCELLING}
            for run in self.state.runs.values()
        ):
            raise ValueError("active run already exists for this session")
        input_message = next(
            (
                item
                for item in self.state.messages[session_id]
                if item.turn_id == turn_id and item.role == "user"
            ),
            None,
        )
        if input_message is None:
            input_message = self._add_message(session_id, turn_id, "user", normalized, None, None)
        elif input_message.content != normalized:
            raise ValueError("run request conflicts with a previous payload")
        limits = limits_for_preset(budget_preset)
        now = _now()
        deadline = (datetime.now(UTC) + timedelta(seconds=limits.max_seconds)).isoformat()
        run = RunSnapshot(
            id=_id(),
            session_id=session_id,
            turn_id=turn_id,
            status=RunStatus.QUEUED,
            provider_id=model.provider_id,
            model_id=model.id,
            adapter_kind=model.adapter_kind,
            upstream_model_id=model.upstream_model_id,
            input_message_id=input_message.id,
            retry_of=retry_of,
            client_request_id=client_request_id,
            max_model_calls=limits.max_model_calls,
            max_tool_calls=limits.max_tool_calls,
            budget_preset=budget_preset,
            max_total_tokens=limits.max_total_tokens,
            max_cost_usd=limits.max_cost_usd,
            deadline_at=deadline,
            cancel_requested_at=None,
            lease_expires_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            recovery_count=0,
            stop_reason=None,
            last_event_sequence=0,
            error_code=None,
            error_message=None,
            created_at=now,
            started_at=None,
            finished_at=None,
        )
        self.state.runs[run.id] = run
        self.state.events[run.id] = []
        self.state.run_messages[run.id] = []
        self._owned_leases.add(run.id)
        self._event(
            run.id,
            RunEventType.QUEUED,
            {"session_id": session_id, "turn_id": turn_id, "status": "queued"},
        )
        return self.state.runs[run.id]

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        return self.state.runs.get(run_id)

    async def get_latest_run_for_session(self, session_id: str) -> RunSnapshot | None:
        return next(
            (
                run
                for run in reversed(tuple(self.state.runs.values()))
                if run.session_id == session_id
            ),
            None,
        )

    async def list_runs_for_session(
        self, session_id: str, offset: int, limit: int
    ) -> list[RunSnapshot]:
        if offset < 0 or not 1 <= limit <= 101:
            raise ValueError("run offset and limit are outside the supported range")
        return [run for run in self.state.runs.values() if run.session_id == session_id][
            offset : offset + limit
        ]

    async def append_run_event(
        self,
        run_id: str,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        event_version: int = 1,
    ) -> RunEvent:
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        if is_lifecycle_event(event_type):
            raise ValueError("run lifecycle events must use transition_run_record")
        if is_terminal_run_status(run.status):
            raise ValueError("cannot append an event to a terminal run")
        json.dumps(data, allow_nan=False)
        return self._event(run_id, event_type, data, event_version=event_version)

    async def list_run_events(
        self, run_id: str, after_sequence: int = 0, *, limit: int = 500
    ) -> list[RunEvent]:
        if after_sequence < 0 or not 1 <= limit <= 501:
            raise ValueError("event cursor and limit are outside the supported range")
        return [
            event for event in self.state.events.get(run_id, []) if event.sequence > after_sequence
        ][:limit]

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
    ) -> RunEvent:
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        validate_run_event_transition(run.status, next_status, event_type)
        now = _now()
        self.state.runs[run_id] = replace(
            run,
            status=next_status,
            error_code=error_code,
            error_message=error_message,
            stop_reason=stop_reason,
            started_at=run.started_at or (now if next_status is RunStatus.RUNNING else None),
            finished_at=now if is_terminal_run_status(next_status) else None,
        )
        if is_terminal_run_status(next_status):
            self._owned_leases.discard(run_id)
        return self._event(run_id, event_type, data)

    async def complete_run(self, run_id: str, content: str) -> tuple[Message, tuple[RunEvent, ...]]:
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        normalized = content.strip()
        if not normalized:
            raise ValueError("assistant response cannot be empty")
        validate_run_event_transition(run.status, RunStatus.COMPLETED, RunEventType.COMPLETED)
        message = self._add_message(
            run.session_id, run.turn_id, "assistant", normalized, run.provider_id, run.model_id
        )
        first = self._event(run_id, RunEventType.ASSISTANT_COMPLETED, {"message_id": message.id})
        second = await self.transition_run_record(
            run_id,
            RunStatus.COMPLETED,
            RunEventType.COMPLETED,
            {"message_id": message.id, "status": "completed", "stop_reason": "final_response"},
            stop_reason="final_response",
        )
        return message, (first, second)

    async def request_run_cancellation(self, run_id: str) -> tuple[RunSnapshot, RunEvent | None]:
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        if run.status in {
            RunStatus.CANCELLING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            return run, None
        data: dict[str, object]
        if run.status is RunStatus.QUEUED:
            next_status, event_type = RunStatus.CANCELLED, RunEventType.CANCELLED
            data = {
                "status": "cancelled",
                "code": "user_cancelled",
                "message": "The run was cancelled.",
            }
            reason = "user_cancelled"
        else:
            next_status, event_type = RunStatus.CANCELLING, RunEventType.CANCELLATION_REQUESTED
            data = {"status": "cancelling"}
            reason = None
            self.state.waiting_tool_call_id.pop(run_id, None)
        self.state.runs[run_id] = replace(
            run, cancel_requested_at=run.cancel_requested_at or _now()
        )
        event = await self.transition_run_record(
            run_id, next_status, event_type, data, stop_reason=reason
        )
        return self.state.runs[run_id], event

    def owns_run_lease(self, run_id: str) -> bool:
        return run_id in self._owned_leases

    async def renew_run_lease(self, run_id: str) -> RunSnapshot:
        run = self.state.runs.get(run_id)
        if run is None or run_id not in self._owned_leases:
            raise ValueError("run lease not owned")
        renewed = replace(
            run, lease_expires_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        )
        self.state.runs[run_id] = renewed
        return renewed

    async def recover_expired_run(self, run_id: str) -> RunSnapshot:
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        if is_terminal_run_status(run.status):
            return run
        self._owned_leases.discard(run_id)
        self.state.runs[run_id] = replace(run, recovery_count=run.recovery_count + 1)
        await self.transition_run_record(
            run_id,
            RunStatus.INTERRUPTED,
            RunEventType.INTERRUPTED,
            {"code": "runtime_restart", "message": "The run was interrupted."},
            error_code="runtime_restart",
            error_message="The run was interrupted.",
            stop_reason="runtime_restart",
        )
        return self.state.runs[run_id]

    async def recover_active_runs(self) -> tuple[RunEvent, ...]:
        recovered: list[RunEvent] = []
        for run in tuple(self.state.runs.values()):
            if run.status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCELLING}:
                await self.recover_expired_run(run.id)
                recovered.append(self.state.events[run.id][-1])
        return tuple(recovered)

    async def create_model_call(
        self,
        run_id: str,
        step_index: int,
        model: ModelDescriptor,
        request_snapshot: dict[str, object],
    ) -> ModelCallRecord:
        if step_index < 1:
            raise ValueError("model call step index must be positive")
        if run_id not in self.state.runs:
            raise ValueError("run not found")
        if run_id in self.state.waiting_tool_call_id:
            raise ValueError("run is not running")
        json.dumps(request_snapshot, allow_nan=False)
        record = ModelCallRecord(
            id=_id(),
            run_id=run_id,
            step_index=step_index,
            provider_id=model.provider_id,
            model_id=model.id,
            adapter_kind=model.adapter_kind,
            status=ModelCallStatus.PENDING,
            request_snapshot=request_snapshot,
            response_snapshot=None,
            provider_response_id=None,
            finish_reason=None,
            input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            cached_tokens=None,
            cache_creation_tokens=None,
            estimated_cost=None,
            error_code=None,
            error_message=None,
            started_at=_now(),
            finished_at=None,
        )
        self.state.model_calls[record.id] = record
        return record

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
    ) -> ModelCallRecord:
        record = self.state.model_calls.get(call_id)
        if record is None:
            raise ValueError("model call not found")
        transition_model_call(record.status, next_status)
        if response_snapshot is not None:
            json.dumps(response_snapshot, allow_nan=False)
        updated = replace(
            record,
            status=next_status,
            response_snapshot=response_snapshot
            if response_snapshot is not None
            else record.response_snapshot,
            provider_response_id=provider_response_id or record.provider_response_id,
            finish_reason=finish_reason or record.finish_reason,
            input_tokens=input_tokens if input_tokens is not None else record.input_tokens,
            output_tokens=output_tokens if output_tokens is not None else record.output_tokens,
            reasoning_tokens=reasoning_tokens
            if reasoning_tokens is not None
            else record.reasoning_tokens,
            cached_tokens=cached_tokens if cached_tokens is not None else record.cached_tokens,
            cache_creation_tokens=(
                cache_creation_tokens
                if cache_creation_tokens is not None
                else record.cache_creation_tokens
            ),
            estimated_cost=estimated_cost if estimated_cost is not None else record.estimated_cost,
            error_code=error_code or record.error_code,
            error_message=error_message or record.error_message,
            finished_at=None if next_status is ModelCallStatus.STREAMING else _now(),
        )
        self.state.model_calls[call_id] = updated
        return updated

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]:
        return sorted(
            (call for call in self.state.model_calls.values() if call.run_id == run_id),
            key=lambda call: call.step_index,
        )

    async def record_assistant_message(
        self,
        run_id: str,
        model_call_id: str,
        message: ModelMessage,
    ) -> tuple[RunMessageRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]:
        if message.role != "assistant" or message.tool_call_id is not None:
            raise ValueError("an assistant run message is required")
        if not message.content.strip() and not message.tool_calls:
            raise ValueError("assistant run message cannot be empty")
        provider_ids = [call.id for call in message.tool_calls]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("tool call IDs must be unique within an assistant message")
        json.dumps([call.arguments for call in message.tool_calls], allow_nan=False)
        continuation = [
            {
                "provider_id": item.provider_id,
                "model_id": item.model_id,
                "payload_json": item.payload_json,
            }
            for item in message.continuation_items
        ]
        if len(json.dumps(continuation, allow_nan=False).encode()) > 4 * 1024 * 1024:
            raise ValueError("assistant continuation exceeds the storage limit")
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        if run.status is not RunStatus.RUNNING or run_id in self.state.waiting_tool_call_id:
            raise ValueError("run is not running")
        model_call = self.state.model_calls.get(model_call_id)
        if (
            model_call is None
            or model_call.run_id != run_id
            or model_call.status is not ModelCallStatus.COMPLETED
        ):
            raise ValueError("completed model call not found for run")
        now = _now()
        assistant = RunMessageRecord(
            id=_id(),
            run_id=run_id,
            ordinal=len(self.state.run_messages[run_id]) + 1,
            role="assistant",
            content=message.content,
            model_call_id=model_call_id,
            tool_call_id=None,
            created_at=now,
            tool_calls=message.tool_calls,
            continuation_items=message.continuation_items,
        )
        self.state.run_messages[run_id].append(assistant)
        calls = tuple(
            ToolCallRecord(
                id=_id(),
                run_id=run_id,
                assistant_message_id=assistant.id,
                call_index=index,
                provider_call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                status=ToolCallStatus.PENDING,
                approval_decision=None,
                approval_decided_at=None,
                created_at=now,
                finished_at=None,
            )
            for index, call in enumerate(message.tool_calls)
        )
        self.state.tool_calls.update((call.id, call) for call in calls)
        events = [
            self._event(
                run_id,
                RunEventType.ASSISTANT_MESSAGE,
                {
                    "message_id": assistant.id,
                    "model_call_id": model_call_id,
                    "content": message.content,
                },
            )
        ]
        events.extend(
            self._event(
                run_id,
                RunEventType.TOOL_CALL,
                {
                    "tool_call_id": call.id,
                    "provider_call_id": call.provider_call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "assistant_message_id": assistant.id,
                },
            )
            for call in calls
        )
        return assistant, calls, tuple(events)

    async def record_tool_result(
        self,
        run_id: str,
        tool_call_id: str,
        content: str,
        *,
        status: ToolCallStatus = ToolCallStatus.COMPLETED,
    ) -> tuple[RunMessageRecord, ToolCallRecord, RunEvent]:
        if status not in {
            ToolCallStatus.COMPLETED,
            ToolCallStatus.FAILED,
            ToolCallStatus.DENIED,
            ToolCallStatus.CANCELLED,
            ToolCallStatus.TIMED_OUT,
        }:
            raise ValueError("tool result requires a terminal status")
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        if run_id in self.state.waiting_tool_call_id or not (
            run.status is RunStatus.RUNNING
            or (run.status is RunStatus.CANCELLING and status is ToolCallStatus.CANCELLED)
        ):
            raise ValueError("run is not running")
        call = self.state.tool_calls.get(tool_call_id)
        if call is None or call.run_id != run_id:
            raise ValueError("tool call not found for run")
        if call.status not in {ToolCallStatus.PENDING, ToolCallStatus.RUNNING}:
            raise ValueError("tool call already has a result")
        if (
            call.approval_decision is ToolApprovalDecision.DENIED
            and status is not ToolCallStatus.DENIED
        ):
            raise ValueError("tool approval was denied")
        now = _now()
        result = RunMessageRecord(
            id=_id(),
            run_id=run_id,
            ordinal=len(self.state.run_messages[run_id]) + 1,
            role="tool",
            content=content,
            model_call_id=None,
            tool_call_id=tool_call_id,
            created_at=now,
        )
        self.state.run_messages[run_id].append(result)
        updated = replace(call, status=status, finished_at=now)
        self.state.tool_calls[tool_call_id] = updated
        event = self._event(
            run_id,
            RunEventType.TOOL_RESULT,
            {
                "tool_call_id": tool_call_id,
                "message_id": result.id,
                "status": status.value,
                "content": content,
            },
        )
        return result, updated, event

    async def list_run_messages(self, run_id: str) -> list[RunMessageRecord]:
        return list(self.state.run_messages.get(run_id, []))

    async def list_tool_calls(self, run_id: str) -> list[ToolCallRecord]:
        return [call for call in self.state.tool_calls.values() if call.run_id == run_id]

    async def request_tool_approval(
        self,
        run_id: str,
        tool_call_id: str,
        preview: dict[str, object] | None,
    ) -> tuple[RunSnapshot, RunEvent]:
        if preview is not None and len(json.dumps(preview, allow_nan=False).encode()) > 128_000:
            raise ValueError("approval preview exceeds the storage limit")
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        if run.status is not RunStatus.RUNNING or run_id in self.state.waiting_tool_call_id:
            raise ValueError("run is not ready to request approval")
        call = self.state.tool_calls.get(tool_call_id)
        if call is None or call.run_id != run_id or call.status is not ToolCallStatus.PENDING:
            raise ValueError("pending tool call not found for approval")
        if call.approval_decision is not None:
            raise ValueError("tool approval has already been decided")
        validate_run_event_transition(
            run.status, RunStatus.WAITING_FOR_APPROVAL, RunEventType.TOOL_APPROVAL_REQUESTED
        )
        self.state.waiting_tool_call_id[run_id] = tool_call_id
        self.state.runs[run_id] = replace(run, status=RunStatus.WAITING_FOR_APPROVAL)
        self.state.tool_calls[tool_call_id] = replace(call, approval_preview=preview)
        event = self._event(
            run_id,
            RunEventType.TOOL_APPROVAL_REQUESTED,
            {
                "tool_call_id": tool_call_id,
                "name": call.name,
                "arguments": call.arguments,
                "preview": preview,
            },
        )
        return self.state.runs[run_id], event

    async def record_tool_approval_decision(
        self,
        run_id: str,
        tool_call_id: str,
        decision: ToolApprovalDecision,
    ) -> tuple[ToolCallRecord, RunEvent | None]:
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        call = self.state.tool_calls.get(tool_call_id)
        if call is None or call.run_id != run_id:
            raise ValueError("tool call not found for run")
        if (
            run.status is not RunStatus.WAITING_FOR_APPROVAL
            or self.state.waiting_tool_call_id.get(run_id) != tool_call_id
        ):
            raise ValueError("run is not waiting for this tool approval")
        if call.approval_decision is not None:
            if call.approval_decision is not decision:
                raise ValueError("tool approval decision already differs")
            return call, None
        if call.status is not ToolCallStatus.PENDING:
            raise ValueError("tool call is no longer pending")
        updated = replace(call, approval_decision=decision, approval_decided_at=_now())
        self.state.tool_calls[tool_call_id] = updated
        event = self._event(
            run_id,
            RunEventType.TOOL_APPROVAL_DECIDED,
            {"tool_call_id": tool_call_id, "decision": decision.value},
        )
        return updated, event

    async def resume_approved_run(self, run_id: str) -> tuple[RunSnapshot, RunEvent]:
        run = self.state.runs.get(run_id)
        if run is None:
            raise ValueError("run not found")
        tool_call_id = self.state.waiting_tool_call_id.get(run_id)
        if run.status is not RunStatus.WAITING_FOR_APPROVAL or tool_call_id is None:
            raise ValueError("run is not waiting for approval")
        call = self.state.tool_calls.get(tool_call_id)
        if (
            call is None
            or call.status is not ToolCallStatus.PENDING
            or call.approval_decision is None
        ):
            raise ValueError("waiting tool call has no approval decision")
        approval = next(
            (
                event
                for event in reversed(self.state.events[run_id])
                if event.event_type is RunEventType.TOOL_APPROVAL_REQUESTED
                and event.data.get("tool_call_id") == tool_call_id
            ),
            None,
        )
        if approval is None:
            raise ValueError("waiting tool call has no approval request")
        validate_run_event_transition(run.status, RunStatus.RUNNING, RunEventType.RESUMED)
        waited = datetime.now(UTC) - datetime.fromisoformat(
            approval.created_at.replace("Z", "+00:00")
        )
        deadline = datetime.fromisoformat(run.deadline_at.replace("Z", "+00:00")) + waited
        updated = replace(
            run,
            status=RunStatus.RUNNING,
            deadline_at=deadline.isoformat().replace("+00:00", "Z"),
        )
        self.state.runs[run_id] = updated
        del self.state.waiting_tool_call_id[run_id]
        event = self._event(
            run_id,
            RunEventType.RESUMED,
            {"tool_call_id": tool_call_id, "deadline_at": updated.deadline_at},
        )
        return self.state.runs[run_id], event

    async def list_decided_approval_runs(self) -> list[str]:
        return [
            run.id
            for run in self.state.runs.values()
            if run.status is RunStatus.WAITING_FOR_APPROVAL
            and run.id in self.state.waiting_tool_call_id
            and any(
                call.id == self.state.waiting_tool_call_id[run.id]
                and call.approval_decision is not None
                for call in self.state.tool_calls.values()
            )
        ]


def memory_repository_factory(
    settings: Settings,
    user_id: UUID,
    _tokens: VerifiedTokenSource,
    _client: httpx.AsyncClient,
) -> MemoryRepository:
    return MemoryRepository(
        settings.data_dir / "accounts" / str(user_id) / "state.db", owner_id=user_id
    )
