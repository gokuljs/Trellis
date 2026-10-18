from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.dependencies import (
    ChatServiceDep,
    RunServiceDep,
    SessionServiceDep,
    correlate_owned_chat,
)
from app.api.telemetry import owned_chat_telemetry
from app.domain.models import Message, Session
from app.domain.runtime import RunSummary


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    workspace_path: str | None
    workspace_ready: bool
    model_id: str | None = None


class SessionModelRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)


class SessionWorkspaceRequest(BaseModel):
    workspace_path: str | None = Field(default=None, max_length=4096)


class MessageResponse(BaseModel):
    id: str
    turn_id: str
    role: str
    content: str
    provider: str | None
    model: str | None
    created_at: str


class AssistantOutputResponse(BaseModel):
    id: str
    turn_id: str
    run_id: str
    model_call_id: str
    step_index: int
    next_batch_index: int
    content: str
    status: str
    stop_reason: str | None
    promoted_message_id: str | None
    created_at: str


class SessionDetailResponse(BaseModel):
    session: SessionResponse
    messages: list[MessageResponse]
    assistant_outputs: list[AssistantOutputResponse] = Field(default_factory=list)


class LatestRunResponse(BaseModel):
    input_message: MessageResponse | None = None
    run_id: str
    turn_id: str
    status: str
    last_sequence: int


class RunSummaryResponse(BaseModel):
    run_id: str
    turn_id: str
    retry_of: str | None
    status: str
    budget_preset: str
    max_model_calls: int
    max_tool_calls: int
    max_total_tokens: int
    max_cost_usd: float
    deadline_at: str
    last_sequence: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    provider_id: str
    model_id: str
    model_call_count: int
    tool_call_count: int
    total_tokens: int | None
    usage_complete: bool
    estimated_cost_usd: float | None
    cost_complete: bool
    stop_reason: str | None
    error_code: str | None
    error_message: str | None


class RunSummariesResponse(BaseModel):
    items: list[RunSummaryResponse]
    next_offset: int | None


class RunEventResponse(BaseModel):
    run_id: str
    sequence: int
    event_type: str
    event_version: int
    data: dict[str, object]
    created_at: str


class RunEventsResponse(BaseModel):
    items: list[RunEventResponse]
    next_after_sequence: int | None


class TurnRequest(BaseModel):
    turn_id: UUID
    content: str = Field(min_length=1, max_length=100_000)


class TurnResponse(BaseModel):
    session: SessionResponse
    user_message: MessageResponse
    assistant_message: MessageResponse


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def serialize_session(session: Session, service: SessionServiceDep) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=session.message_count,
        workspace_path=session.workspace_path if service.workspace_enabled else None,
        workspace_ready=await service.workspace_ready(session),
        model_id=session.model_id,
    )


def serialize_message(message: Message) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        turn_id=message.turn_id,
        role=message.role,
        content=message.content,
        provider=message.provider,
        model=message.model,
        created_at=message.created_at,
    )


def serialize_run_summary(summary: RunSummary) -> RunSummaryResponse:
    run = summary.run
    lease_expired = run.stop_reason == "lease_expired"
    return RunSummaryResponse(
        run_id=run.id,
        turn_id=run.turn_id,
        retry_of=run.retry_of,
        status=run.status.value,
        budget_preset=run.budget_preset,
        max_model_calls=run.max_model_calls,
        max_tool_calls=run.max_tool_calls,
        max_total_tokens=run.max_total_tokens,
        max_cost_usd=run.max_cost_usd,
        deadline_at=run.deadline_at,
        last_sequence=run.last_event_sequence,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        provider_id=run.provider_id,
        model_id=run.model_id,
        model_call_count=summary.model_call_count,
        tool_call_count=summary.tool_call_count,
        total_tokens=summary.total_tokens,
        usage_complete=summary.usage_complete,
        estimated_cost_usd=summary.estimated_cost_usd,
        cost_complete=summary.cost_complete,
        stop_reason=run.stop_reason,
        error_code=run.error_code or ("lease_expired" if lease_expired else None),
        error_message=run.error_message
        or (
            "The API stopped before this run finished. Start a new run to continue."
            if lease_expired
            else None
        ),
    )


@router.get("")
async def list_sessions(service: SessionServiceDep) -> list[SessionResponse]:
    return [await serialize_session(session, service) for session in await service.list_sessions()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    request: Request, service: SessionServiceDep, payload: SessionWorkspaceRequest | None = None
) -> SessionResponse:
    session = await service.create(payload.workspace_path if payload else None)
    with owned_chat_telemetry(request.scope, session):
        return await serialize_session(session, service)


@router.put("/{session_id}/workspace", dependencies=[Depends(correlate_owned_chat)])
async def set_session_workspace(
    session_id: str, payload: SessionWorkspaceRequest, service: SessionServiceDep
) -> SessionResponse:
    return await serialize_session(
        await service.set_workspace(session_id, payload.workspace_path), service
    )


@router.get("/{session_id}", dependencies=[Depends(correlate_owned_chat)])
async def get_session(session_id: str, service: SessionServiceDep) -> SessionDetailResponse:
    detail = await service.get(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetailResponse(
        session=await serialize_session(detail.session, service),
        messages=[serialize_message(message) for message in detail.messages],
        assistant_outputs=[
            AssistantOutputResponse.model_validate(output, from_attributes=True)
            for output in detail.assistant_outputs
        ],
    )


@router.put("/{session_id}/model", dependencies=[Depends(correlate_owned_chat)])
async def select_session_model(
    session_id: str, payload: SessionModelRequest, service: SessionServiceDep
) -> SessionResponse:
    return await serialize_session(
        await service.select_model(session_id, payload.model_id), service
    )


@router.get("/{session_id}/runs/latest", dependencies=[Depends(correlate_owned_chat)])
async def get_latest_run(
    session_id: str, service: RunServiceDep, sessions: SessionServiceDep
) -> LatestRunResponse | None:
    run = await service.get_latest_run_for_session(session_id)
    input_message = await sessions.get_message(session_id, run.input_message_id) if run else None
    return (
        None
        if run is None
        else LatestRunResponse(
            input_message=serialize_message(input_message) if input_message else None,
            run_id=run.id,
            turn_id=run.turn_id,
            status=run.status.value,
            last_sequence=run.last_event_sequence,
        )
    )


@router.get("/{session_id}/runs", dependencies=[Depends(correlate_owned_chat)])
async def list_session_runs(
    session_id: str,
    service: RunServiceDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    turn_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    newest_first: bool = False,
) -> RunSummariesResponse:
    runs, next_offset = await service.list_session_runs(
        session_id, offset, limit, turn_id=turn_id, newest_first=newest_first
    )
    return RunSummariesResponse(
        items=[serialize_run_summary(summary) for summary in runs],
        next_offset=next_offset,
    )


@router.get("/{session_id}/runs/{run_id}/events", dependencies=[Depends(correlate_owned_chat)])
async def list_session_run_events(
    session_id: str,
    run_id: str,
    service: RunServiceDep,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> RunEventsResponse:
    events, next_sequence = await service.list_session_run_events(
        session_id, run_id, after_sequence, limit
    )
    return RunEventsResponse(
        items=[
            RunEventResponse(
                run_id=event.run_id,
                sequence=event.sequence,
                event_type=event.event_type.value,
                event_version=event.event_version,
                data=event.data,
                created_at=event.created_at,
            )
            for event in events
        ],
        next_after_sequence=next_sequence,
    )


@router.post(
    "/{session_id}/turns",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(correlate_owned_chat)],
)
async def complete_turn(
    session_id: str,
    payload: TurnRequest,
    chat_service: ChatServiceDep,
    session_service: SessionServiceDep,
) -> TurnResponse:
    result = await chat_service.complete_turn(session_id, str(payload.turn_id), payload.content)
    return TurnResponse(
        session=await serialize_session(result.session, session_service),
        user_message=serialize_message(result.user_message),
        assistant_message=serialize_message(result.assistant_message),
    )
