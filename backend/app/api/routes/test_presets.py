"""Session-scoped test preset management."""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr

from app.application.test_presets import TestPresetService


class TestPresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    command: SecretStr
    cwd: str = Field(default=".", min_length=1, max_length=4096)


class TestPresetResponse(BaseModel):
    name: str
    command: str
    cwd: str


def get_test_preset_service(request: Request) -> TestPresetService:
    return request.app.state.test_preset_service


Service = Annotated[TestPresetService, Depends(get_test_preset_service)]
router = APIRouter(prefix="/api/sessions/{session_id}/test-presets", tags=["test-presets"])


@router.get("")
async def list_test_presets(session_id: str, service: Service) -> list[TestPresetResponse]:
    return [TestPresetResponse(**asdict(item)) for item in await service.list_presets(session_id)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def save_test_preset(
    session_id: str, payload: TestPresetRequest, service: Service
) -> TestPresetResponse:
    preset = await service.save(
        session_id, payload.name, payload.command.get_secret_value(), payload.cwd
    )
    return TestPresetResponse(**asdict(preset))


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_test_preset(session_id: str, name: str, service: Service) -> Response:
    if not await service.delete(session_id, name):
        raise HTTPException(status_code=404, detail="Test command not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
