from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.dependencies import WebEvidenceServiceDep
from app.domain.web import MAX_SOURCE_LINES


class SourceResponse(BaseModel):
    id: str
    session_id: str
    run_id: str
    tool_call_id: str
    requested_url: str | None
    final_url: str | None
    canonical_url: str | None
    title: str | None
    publisher: str | None
    published_at: str | None
    fetched_at: str | None
    extractor_version: str | None
    content_hash: str | None
    line_count: int
    byte_count: int
    status: str
    error_code: str | None
    created_at: str


class SourcesResponse(BaseModel):
    items: list[SourceResponse]
    next_offset: int | None


class SourceLinesResponse(BaseModel):
    source: SourceResponse
    start_line: int
    lines: list[str]
    total_lines: int


class ClearSourcesResponse(BaseModel):
    evidence_epoch: int
    cleared_count: int


router = APIRouter(prefix="/api/sessions", tags=["sources"])


@router.get("/{session_id}/sources")
async def list_sources(
    session_id: UUID,
    service: WebEvidenceServiceDep,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SourcesResponse:
    result = await service.list_sources(str(session_id), offset, limit)
    return SourcesResponse(
        items=[SourceResponse.model_validate(item, from_attributes=True) for item in result.items],
        next_offset=result.next_offset,
    )


@router.get("/{session_id}/sources/{source_id}")
async def read_source(
    session_id: UUID,
    source_id: UUID,
    service: WebEvidenceServiceDep,
    start_line: Annotated[int, Query(ge=1, le=MAX_SOURCE_LINES)] = 1,
    max_lines: Annotated[int, Query(ge=1, le=200)] = 100,
) -> SourceLinesResponse:
    result = await service.read_source(str(session_id), str(source_id), start_line, max_lines)
    return SourceLinesResponse(
        source=SourceResponse.model_validate(result.source, from_attributes=True),
        start_line=result.start_line,
        lines=list(result.lines),
        total_lines=result.total_lines,
    )


@router.delete("/{session_id}/sources")
async def clear_sources(session_id: UUID, service: WebEvidenceServiceDep) -> ClearSourcesResponse:
    result = await service.clear_sources(str(session_id))
    return ClearSourcesResponse.model_validate(result, from_attributes=True)
