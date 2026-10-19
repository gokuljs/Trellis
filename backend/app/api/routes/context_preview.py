from contextlib import nullcontext
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from app.api.dependencies import AccountDep, ContextPreviewServiceDep
from app.api.telemetry import owned_chat_telemetry
from app.application.budgets import BudgetPreset


class ContextPreviewRequest(BaseModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    draft: str = Field(max_length=32_000)
    budget_preset: BudgetPreset

    @field_validator("draft")
    @classmethod
    def scalar_draft(cls, value: str) -> str:
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("Draft must contain Unicode scalar values.")
        return value


class ContextPreviewResponse(BaseModel):
    model_id: str
    context_window_tokens: int | None
    input_tokens: int | None
    count_method: Literal["provider", "utf8_bytes_estimate_v1"]
    output_reserve_tokens: int | None
    safety_margin_tokens: int | None
    usable_input_tokens: int | None
    compaction_threshold_tokens: int | None
    percentage: float | None
    status: Literal["ready", "compaction_required", "compacting", "unavailable"]
    checkpoint_id: str | None


router = APIRouter(prefix="/api/context-preview", tags=["context"])


@router.post("")
async def preview_context(
    request: Request,
    payload: ContextPreviewRequest,
    service: ContextPreviewServiceDep,
    account: AccountDep,
) -> ContextPreviewResponse:
    session = (
        await account.database.get_session(payload.session_id)
        if payload.session_id is not None
        else None
    )
    with owned_chat_telemetry(request.scope, session) if session else nullcontext():
        result = await service.preview(
            session_id=payload.session_id,
            model_id=payload.model_id,
            draft=payload.draft,
            budget_preset=payload.budget_preset,
        )
        return ContextPreviewResponse.model_validate(result, from_attributes=True)
