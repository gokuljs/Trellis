from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import WorkspacePickerDep


class WorkspacePickerResponse(BaseModel):
    path: str | None


router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.post("/pick", response_model=WorkspacePickerResponse)
async def pick_workspace(picker: WorkspacePickerDep) -> WorkspacePickerResponse:
    return WorkspacePickerResponse(path=await picker.pick_directory())
