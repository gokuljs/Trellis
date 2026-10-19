import asyncio

import httpx

from app.core.config import Settings
from app.core.supabase import project_base_url

_READINESS_TIMEOUT_SECONDS = 5.0


class SupabaseReadiness:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._base_url = project_base_url(settings)
        self._publishable_key = settings.supabase_publishable_key or ""
        self._client = client

    async def is_ready(self) -> bool:
        if self._base_url is None:
            return False
        try:
            async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS):
                response = await self._client.post(
                    f"{self._base_url}/rest/v1/rpc/readiness",
                    headers={
                        "apikey": self._publishable_key,
                        "Content-Profile": "trellis",
                        "Accept": "application/json",
                    },
                    json={},
                    timeout=httpx.Timeout(5, connect=3),
                    follow_redirects=False,
                )
            return response.status_code == 200 and response.json() == {"status": "ready"}
        except httpx.RequestError, TimeoutError, ValueError:
            return False
