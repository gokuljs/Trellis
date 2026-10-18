from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.dependencies import SessionServiceDep, correlate_owned_chat
from app.api.routes.sessions import (
    AssistantOutputResponse,
    SessionDetailResponse,
    SessionResponse,
    serialize_message,
    serialize_session,
)


class TimelineRowResponse(BaseModel):
    kind: Literal["message", "assistant_output"]
    id: str


class HistoryPageResponse(SessionDetailResponse):
    timeline_order: list[TimelineRowResponse]
    next_before_cursor: str | None


class SessionPageResponse(BaseModel):
    items: list[SessionResponse]
    next_before_cursor: str | None


router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/session-pages")
async def read_session_page(
    service: SessionServiceDep,
    before_cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> SessionPageResponse:
    page = await service.read_session_page(before_cursor, limit)
    return SessionPageResponse(
        items=[await serialize_session(item, service) for item in page.items],
        next_before_cursor=page.next_before_cursor,
    )


@router.get("/sessions/{session_id}/history", dependencies=[Depends(correlate_owned_chat)])
async def read_history_page(
    session_id: str,
    service: SessionServiceDep,
    before_cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> HistoryPageResponse:
    session, page = await service.read_history_page(session_id, before_cursor, limit)
    return HistoryPageResponse(
        session=await serialize_session(session, service),
        messages=[serialize_message(message) for message in page.messages],
        assistant_outputs=[
            AssistantOutputResponse.model_validate(output, from_attributes=True)
            for output in page.assistant_outputs
        ],
        timeline_order=[
            TimelineRowResponse.model_validate(row, from_attributes=True)
            for row in page.timeline_order
        ],
        next_before_cursor=page.next_before_cursor,
    )
