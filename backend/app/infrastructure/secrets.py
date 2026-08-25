import asyncio
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

from app.domain.models import ProviderName

ENV_NAMES: dict[ProviderName, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def get(self, provider: ProviderName) -> str | None:
        values = await asyncio.to_thread(dotenv_values, self.path)
        value = values.get(ENV_NAMES[provider])
        return value if isinstance(value, str) and value else None

    async def set(self, provider: ProviderName, value: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._prepare_path)
            await asyncio.to_thread(
                set_key,
                self.path,
                ENV_NAMES[provider],
                value,
                quote_mode="always",
            )
            await asyncio.to_thread(self.path.chmod, 0o600)

    async def delete(self, provider: ProviderName) -> None:
        async with self._lock:
            if not await asyncio.to_thread(self.path.exists):
                return
            await asyncio.to_thread(unset_key, self.path, ENV_NAMES[provider])
            await asyncio.to_thread(self.path.chmod, 0o600)

    async def status(self, provider: ProviderName) -> tuple[bool, str | None]:
        value = await self.get(provider)
        if value is None:
            return False, None
        return True, f"••••{value[-4:]}"

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
