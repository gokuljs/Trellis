from typing import Annotated

from fastapi import Depends, Request

from app.api.auth import VerifiedUser
from app.application.chat import ChatService
from app.application.onboarding import OnboardingService
from app.application.profile import ProfileService
from app.application.runs import RunService
from app.application.sessions import SessionService
from app.application.settings import SettingsService
from app.application.workspaces import WorkspacePickerService
from app.infrastructure.accounts import AccountContext


async def get_current_user(request: Request) -> VerifiedUser:
    return await request.app.state.auth_verifier.verify_bearer(request.headers.get("Authorization"))


CurrentUserDep = Annotated[VerifiedUser, Depends(get_current_user)]


async def get_account_context(request: Request, verified: CurrentUserDep) -> AccountContext:
    authorization = request.headers.get("Authorization", "")
    _, _, token = authorization.partition(" ")
    return await request.app.state.account_registry.get(
        verified.id, access_token=token, expires_at=verified.expires_at
    )


AccountDep = Annotated[AccountContext, Depends(get_account_context)]


def get_chat_service(account: AccountDep) -> ChatService:
    return account.chat_service


def get_profile_service(account: AccountDep) -> ProfileService:
    return account.profile_service


def get_session_service(account: AccountDep) -> SessionService:
    return account.session_service


def get_settings_service(account: AccountDep) -> SettingsService:
    return account.settings_service


def get_onboarding_service(account: AccountDep) -> OnboardingService:
    return account.onboarding_service


def get_run_service(account: AccountDep) -> RunService:
    return account.run_service


def get_workspace_picker(request: Request, _account: AccountDep) -> WorkspacePickerService:
    return request.app.state.workspace_picker


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
SettingsServiceDep = Annotated[SettingsService, Depends(get_settings_service)]
OnboardingServiceDep = Annotated[OnboardingService, Depends(get_onboarding_service)]
RunServiceDep = Annotated[RunService, Depends(get_run_service)]
WorkspacePickerDep = Annotated[WorkspacePickerService, Depends(get_workspace_picker)]
