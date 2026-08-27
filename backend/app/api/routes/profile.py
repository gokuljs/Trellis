from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field

from app.api.dependencies import ProfileServiceDep


class ProfileResponse(BaseModel):
    id: str
    display_name: str | None
    email: str | None
    created_at: str
    updated_at: str


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None


router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("")
async def get_profile(service: ProfileServiceDep) -> ProfileResponse:
    profile = await service.get()
    return ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        email=profile.email,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.put("")
async def update_profile(payload: ProfileUpdate, service: ProfileServiceDep) -> ProfileResponse:
    profile = await service.update(
        display_name=payload.display_name,
        email=str(payload.email) if payload.email else None,
    )
    return ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        email=profile.email,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )
