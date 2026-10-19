from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

from app.application.errors import ProviderError
from app.application.runs import RunService
from app.domain.context import ContextPreflight
from app.domain.models import ModelDescriptor, Session, UserProfile
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
    ToolCallRecord,
)

CANARY = "private-runtime-body-729483"
MODEL = ModelDescriptor(
    "openai:gpt-5.5", "openai", "OpenAI", "responses", "gpt-5.5", "GPT", False, True, True, True
)


def run_record(status: RunStatus = RunStatus.QUEUED) -> RunSnapshot:
    now = datetime.now(UTC)
    return RunSnapshot(
        "c0000000-0000-4000-8000-000000000003",
        "a0000000-0000-4000-8000-000000000001",
        "b0000000-0000-4000-8000-000000000002",
        status,
        "openai",
        MODEL.id,
        MODEL.adapter_kind,
        MODEL.upstream_model_id,
        "input",
        None,
        "request",
        10,
        10,
        "longer",
        100000,
        10,
        (now + timedelta(minutes=5)).isoformat(),
        None,
        None,
        0,
        None,
        0,
        None,
        None,
        now.isoformat(),
        None,
        None,
    )


def model_record() -> ModelCallRecord:
    return ModelCallRecord(
        "d0000000-0000-4000-8000-000000000004",
        "c0000000-0000-4000-8000-000000000003",
        1,
        "openai",
        MODEL.id,
        MODEL.adapter_kind,
        ModelCallStatus.PENDING,
        {},
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        datetime.now(UTC).isoformat(),
        None,
    )


def request() -> ModelRequest:
    return ModelRequest(
        "openai",
        MODEL.id,
        MODEL.adapter_kind,
        MODEL.upstream_model_id,
        (ModelMessage("user", CANARY),),
        100,
    )


def plan() -> ContextPreflight:
    return ContextPreflight(
        request(), (), (), (), 100, "provider", "hash", 100, 1, 10000, True, False
    )


class Repository:
    def __init__(self) -> None:
        self.run = run_record()
        self.calls: list[ModelCallRecord] = []
        self.events: list[RunEvent] = []

    async def create_run(self, *args, **kwargs) -> RunSnapshot:
        return self.run

    async def get_run(self, _run_id: str) -> RunSnapshot:
        return self.run

    async def list_model_calls(self, _run_id: str) -> list[ModelCallRecord]:
        return self.calls.copy()

    async def list_tool_calls(self, _run_id: str) -> list[ToolCallRecord]:
        return []

    async def append_run_event(self, run_id, event_type, data) -> RunEvent:
        event = RunEvent(run_id, len(self.events) + 1, event_type, 1, data, "now")
        self.events.append(event)
        return event

    async def transition_run_record(self, run_id, status, event_type, data, **kwargs) -> RunEvent:
        self.run = replace(self.run, status=status)
        return await self.append_run_event(run_id, event_type, data)

    async def resume_approved_run(self, run_id) -> tuple[RunSnapshot, RunEvent]:
        self.run = replace(self.run, status=RunStatus.RUNNING)
        return self.run, await self.append_run_event(run_id, RunEventType.RESUMED, {})

    async def create_model_call(self, *args, **kwargs) -> ModelCallRecord:
        call = model_record()
        self.calls.append(call)
        return call

    async def update_model_call(self, call_id, status, **kwargs) -> ModelCallRecord:
        call = next(call for call in self.calls if call.id == call_id)
        fields = {key: value for key, value in kwargs.items() if key in call.__dataclass_fields__}
        call = replace(call, status=status, **fields)
        self.calls = [call]
        return call

    async def append_assistant_batch(self, run_id, call_id, index, text):
        return None, await self.append_run_event(
            run_id, RunEventType.ASSISTANT_DELTA, {"text": text}
        )

    async def complete_run(self, run_id, content):
        event = await self.transition_run_record(
            run_id, RunStatus.COMPLETED, RunEventType.COMPLETED, {}
        )
        return None, (event,)


def service(repository, provider) -> RunService:
    sessions = Mock()
    sessions.get_session = AsyncMock(
        return_value=Session(
            "a0000000-0000-4000-8000-000000000001", "user", "title", "now", "now", 0
        )
    )
    settings = Mock()
    settings.list_models = AsyncMock(return_value=[MODEL])
    settings.get_selected_model_id = AsyncMock(return_value=MODEL.id)
    settings.get_default_budget_preset = AsyncMock(return_value="longer")
    profiles = Mock()
    profiles.get_profile = AsyncMock(return_value=UserProfile("user", None, None, "now", "now"))
    secrets = Mock()
    secrets.get = AsyncMock(return_value=CANARY)
    return RunService(
        repository, sessions, settings, profiles, secrets, {MODEL.adapter_kind: provider}
    )


class Provider:
    def __init__(self, failure: bool = False) -> None:
        self.failure = failure
        self.closed = False

    async def stream(self, *args):
        try:
            yield ModelStreamEvent("text_delta", text="answer")
            yield ModelStreamEvent(
                "usage",
                input_tokens=100,
                output_tokens=10,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
            if self.failure:
                raise ProviderError("provider_unavailable", CANARY)
            yield ModelStreamEvent("completed", finish_kind="final")
        finally:
            self.closed = True
