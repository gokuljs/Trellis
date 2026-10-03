from typing import Annotated

from fastapi import Depends, Request

from app.application.chat import ChatService
from app.application.onboarding import OnboardingService
from app.application.profile import ProfileService
from app.application.sessions import SessionService
from app.application.settings import SettingsService


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service


def get_profile_service(request: Request) -> ProfileService:
    return request.app.state.profile_service


def get_session_service(request: Request) -> SessionService:
    return request.app.state.session_service


def get_settings_service(request: Request) -> SettingsService:
    return request.app.state.settings_service


def get_onboarding_service(request: Request) -> OnboardingService:
    return request.app.state.onboarding_service


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
SettingsServiceDep = Annotated[SettingsService, Depends(get_settings_service)]
OnboardingServiceDep = Annotated[OnboardingService, Depends(get_onboarding_service)]
