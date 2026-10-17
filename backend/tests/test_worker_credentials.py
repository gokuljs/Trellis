import asyncio
from pathlib import Path

import pytest

from app.application.errors import ApplicationError
from app.infrastructure.server_credentials import ServerCredentialStore
from app.infrastructure.worker_credentials import AssignedCredentialStore
from tests.support import make_settings


class Authority:
    def __init__(self) -> None:
        self.active = True

    async def assert_provider_access(self, provider: str) -> str:
        if not self.active or provider != "openai":
            raise PermissionError("Assignment denied")
        return "assigned-owner"


def test_credentials_require_live_assigned_provider(tmp_path: Path) -> None:
    async def scenario():
        authority = Authority()
        credentials = AssignedCredentialStore(
            authority,
            ServerCredentialStore(
                make_settings(
                    tmp_path, openai_api_key="assigned-key", anthropic_api_key="other-key"
                )
            ),
        )
        assert await credentials.get("openai") == "assigned-key"
        with pytest.raises(PermissionError):
            await credentials.get("anthropic")
        authority.active = False
        with pytest.raises(PermissionError):
            await credentials.get("openai")
        with pytest.raises(ApplicationError, match="listing is unavailable"):
            await credentials.status("openai")

    asyncio.run(scenario())
