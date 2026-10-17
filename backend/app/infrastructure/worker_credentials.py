from typing import Protocol

from app.application.errors import ApplicationError
from app.application.ports import SecretStorePort
from app.domain.models import ProviderName


class AssignmentAuthority(Protocol):
    async def assert_provider_access(self, provider: str) -> str: ...


class AssignedCredentialStore:
    def __init__(self, authority: AssignmentAuthority, credentials: SecretStorePort) -> None:
        self._authority = authority
        self._credentials = credentials

    async def get(self, provider: ProviderName) -> str | None:
        await self._authority.assert_provider_access(provider)
        return await self._credentials.get(provider)

    async def status(self, provider: ProviderName) -> tuple[bool, None]:
        raise ApplicationError("worker_scope_denied", "Worker credential listing is unavailable.")
