from types import MappingProxyType

from pydantic import SecretStr

from app.core.config import Settings
from app.domain.models import ProviderName


class ServerCredentialStore:
    """An immutable process-start snapshot of operator-managed provider keys."""

    def __init__(self, settings: Settings) -> None:
        values: dict[ProviderName, SecretStr] = {}
        for provider, configured in (
            ("openai", settings.openai_api_key),
            ("anthropic", settings.anthropic_api_key),
        ):
            value = configured.get_secret_value().strip() if configured is not None else ""
            if value:
                values[provider] = SecretStr(value)
        self._values = MappingProxyType(values)

    async def get(self, provider: ProviderName) -> str | None:
        value = self._values.get(provider)
        return value.get_secret_value() if value is not None else None

    async def status(self, provider: ProviderName) -> tuple[bool, None]:
        return provider in self._values, None
