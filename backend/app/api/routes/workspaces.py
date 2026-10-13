from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.dependencies import CurrentUserDep
from app.application.errors import ApplicationError
from app.domain.capabilities import RuntimeCapabilities


class WorkspacePickerResponse(BaseModel):
    path: str | None


router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.post("/pick", response_model=WorkspacePickerResponse)
async def pick_workspace(request: Request, _user: CurrentUserDep) -> WorkspacePickerResponse:
    capabilities = RuntimeCapabilities(request.app.state.settings.enabled_capabilities)
    if not capabilities.workspace_enabled:
        raise ApplicationError("capability_disabled", "Local workspace access is disabled.")
    picker = request.app.state.workspace_picker
    if picker is None:
        raise ApplicationError("workspace_picker_unavailable", "The folder picker is unavailable.")
    return WorkspacePickerResponse(path=await picker.pick_directory())
