from app.application.ports import ProfileRepository
from app.domain.models import UserProfile


class ProfileService:
    def __init__(self, repository: ProfileRepository) -> None:
        self._repository = repository

    async def get(self) -> UserProfile:
        return await self._repository.get_profile()

    async def update(self, display_name: str | None, email: str | None) -> UserProfile:
        normalized_name = display_name.strip() if display_name else None
        return await self._repository.update_profile(normalized_name or None, email)
