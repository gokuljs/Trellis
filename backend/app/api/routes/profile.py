from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field

from app.api.dependencies import DatabaseDep


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
async def get_profile(database: DatabaseDep) -> ProfileResponse:
    profile = await database.get_profile()
    return ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        email=profile.email,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.put("")
async def update_profile(payload: ProfileUpdate, database: DatabaseDep) -> ProfileResponse:
    display_name = payload.display_name.strip() if payload.display_name else None
    profile = await database.update_profile(
        display_name=display_name or None,
        email=str(payload.email) if payload.email else None,
    )
    return ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        email=profile.email,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )
