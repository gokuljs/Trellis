from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field, SecretStr

from app.api.dependencies import OnboardingServiceDep
from app.domain.models import OnboardingStep


class OnboardingStateResponse(BaseModel):
    current_step: OnboardingStep
    completed: bool


class OnboardingProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    email: EmailStr


class OnboardingModelUpdate(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None


router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def serialize_progress(current_step: OnboardingStep, completed: bool) -> OnboardingStateResponse:
    return OnboardingStateResponse(current_step=current_step, completed=completed)


@router.get("")
async def get_onboarding(service: OnboardingServiceDep) -> OnboardingStateResponse:
    progress = await service.get()
    return serialize_progress(progress.current_step, progress.completed)


@router.put("/steps/intro")
async def complete_intro(service: OnboardingServiceDep) -> OnboardingStateResponse:
    progress = await service.complete_intro()
    return serialize_progress(progress.current_step, progress.completed)


@router.put("/steps/profile")
async def save_profile(
    payload: OnboardingProfileUpdate,
    service: OnboardingServiceDep,
) -> OnboardingStateResponse:
    _, progress = await service.save_profile(
        payload.display_name,
        str(payload.email),
    )
    return serialize_progress(progress.current_step, progress.completed)


@router.put("/steps/model")
async def save_model(
    payload: OnboardingModelUpdate,
    service: OnboardingServiceDep,
) -> OnboardingStateResponse:
    api_key = payload.api_key.get_secret_value() if payload.api_key else None
    progress = await service.save_model(payload.model_id, api_key)
    return serialize_progress(progress.current_step, progress.completed)
