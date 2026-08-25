from app.application.errors import ApplicationError
from app.application.ports import SecretStorePort, SettingsRepository
from app.domain.models import AppSettings, ProviderName, ProviderStatus

PROVIDERS: dict[ProviderName, tuple[str, str]] = {
    "openai": ("OpenAI", "gpt-5.5"),
    "anthropic": ("Anthropic", "claude-sonnet-5"),
}


class SettingsService:
    def __init__(self, repository: SettingsRepository, secret_store: SecretStorePort) -> None:
        self._repository = repository
        self._secret_store = secret_store

    async def get(self) -> AppSettings:
        statuses: list[ProviderStatus] = []
        for provider, (name, model) in PROVIDERS.items():
            configured, key_hint = await self._secret_store.status(provider)
            statuses.append(
                ProviderStatus(
                    id=provider,
                    name=name,
                    model=model,
                    configured=configured,
                    key_hint=key_hint,
                )
            )
        return AppSettings(
            selected_provider=await self._repository.get_selected_provider(),
            providers=statuses,
        )

    async def select_provider(self, provider: ProviderName) -> AppSettings:
        await self._repository.set_selected_provider(provider)
        return await self.get()

    async def save_api_key(self, provider: ProviderName, api_key: str) -> AppSettings:
        if not api_key or len(api_key) > 10_000:
            raise ApplicationError(
                "invalid_api_key",
                "API key must contain 1 to 10,000 characters",
            )
        await self._secret_store.set(provider, api_key)
        return await self.get()

    async def remove_api_key(self, provider: ProviderName) -> AppSettings:
        await self._secret_store.delete(provider)
        return await self.get()
