from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.dependencies import ChatServiceDep, RunServiceDep, SessionServiceDep
from app.domain.models import Message, Session


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    workspace_path: str | None


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


class SessionDetailResponse(BaseModel):
    session: SessionResponse
    messages: list[MessageResponse]


class LatestRunResponse(BaseModel):
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


def serialize_session(session: Session) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=session.message_count,
        workspace_path=session.workspace_path,
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


@router.get("")
async def list_sessions(service: SessionServiceDep) -> list[SessionResponse]:
    return [serialize_session(session) for session in await service.list_sessions()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    service: SessionServiceDep, payload: SessionWorkspaceRequest | None = None
) -> SessionResponse:
    return serialize_session(await service.create(payload.workspace_path if payload else None))


@router.put("/{session_id}/workspace")
async def set_session_workspace(
    session_id: str, payload: SessionWorkspaceRequest, service: SessionServiceDep
) -> SessionResponse:
    return serialize_session(await service.set_workspace(session_id, payload.workspace_path))


@router.get("/{session_id}")
async def get_session(session_id: str, service: SessionServiceDep) -> SessionDetailResponse:
    detail = await service.get(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetailResponse(
        session=serialize_session(detail.session),
        messages=[serialize_message(message) for message in detail.messages],
    )


@router.get("/{session_id}/runs/latest")
async def get_latest_run(session_id: str, service: RunServiceDep) -> LatestRunResponse | None:
    run = await service.get_latest_run_for_session(session_id)
    return (
        None
        if run is None
        else LatestRunResponse(
            run_id=run.id,
            turn_id=run.turn_id,
            status=run.status.value,
            last_sequence=run.last_event_sequence,
        )
    )


@router.get("/{session_id}/runs")
async def list_session_runs(
    session_id: str,
    service: RunServiceDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> RunSummariesResponse:
    runs, next_offset = await service.list_session_runs(session_id, offset, limit)
    return RunSummariesResponse(
        items=[
            RunSummaryResponse(
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
            )
            for run in runs
        ],
        next_offset=next_offset,
    )


@router.get("/{session_id}/runs/{run_id}/events")
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


@router.post("/{session_id}/turns", status_code=status.HTTP_201_CREATED)
async def complete_turn(
    session_id: str,
    payload: TurnRequest,
    chat_service: ChatServiceDep,
) -> TurnResponse:
    result = await chat_service.complete_turn(session_id, str(payload.turn_id), payload.content)
    return TurnResponse(
        session=serialize_session(result.session),
        user_message=serialize_message(result.user_message),
        assistant_message=serialize_message(result.assistant_message),
    )
