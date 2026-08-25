from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.dependencies import ChatServiceDep, SessionServiceDep
from app.domain.models import Message, Session


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


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
async def create_session(service: SessionServiceDep) -> SessionResponse:
    return serialize_session(await service.create())


@router.get("/{session_id}")
async def get_session(session_id: str, service: SessionServiceDep) -> SessionDetailResponse:
    detail = await service.get(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetailResponse(
        session=serialize_session(detail.session),
        messages=[serialize_message(message) for message in detail.messages],
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
