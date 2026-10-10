from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import CurrentUserDep, ProfileServiceDep


class ProfileResponse(BaseModel):
    id: str
    display_name: str | None
    email: str | None
    created_at: str
    updated_at: str


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=100)


router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("")
async def get_profile(service: ProfileServiceDep, user: CurrentUserDep) -> ProfileResponse:
    profile = await service.get()
    return ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        email=user.email,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.put("")
async def update_profile(
    payload: ProfileUpdate, service: ProfileServiceDep, user: CurrentUserDep
) -> ProfileResponse:
    profile = await service.update(
        display_name=payload.display_name,
        email=user.email,
    )
    return ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        email=user.email,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )
