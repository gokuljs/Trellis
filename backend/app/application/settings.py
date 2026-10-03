from app.application.errors import ApplicationError
from app.application.ports import SecretStorePort, SettingsRepository
from app.domain.models import AppSettings, ModelId, ModelStatus, ProviderName, ProviderStatus


class SettingsService:
    def __init__(self, repository: SettingsRepository, secret_store: SecretStorePort) -> None:
        self._repository = repository
        self._secret_store = secret_store

    async def get(self) -> AppSettings:
        descriptors = await self._repository.list_models()
        provider_credentials: dict[ProviderName, tuple[bool, str | None]] = {}
        statuses: list[ModelStatus] = []
        providers: list[ProviderStatus] = []
        for model in descriptors:
            credential = provider_credentials.get(model.provider_id)
            if credential is None:
                credential = await self._secret_store.status(model.provider_id)
                provider_credentials[model.provider_id] = credential
                providers.append(
                    ProviderStatus(
                        id=model.provider_id,
                        name=model.provider_name,
                        model=model.name,
                        configured=credential[0],
                        key_hint=credential[1],
                    )
                )
            statuses.append(
                ModelStatus(
                    id=model.id,
                    provider_id=model.provider_id,
                    provider_name=model.provider_name,
                    adapter_kind=model.adapter_kind,
                    upstream_model_id=model.upstream_model_id,
                    name=model.name,
                    requires_api_key=model.requires_api_key,
                    supports_streaming=model.supports_streaming,
                    supports_tools=model.supports_tools,
                    configured=credential[0],
                    key_hint=credential[1],
                )
            )
        return AppSettings(
            selected_provider=await self._repository.get_selected_provider(),
            providers=providers,
            selected_model_id=await self._repository.get_selected_model_id(),
            models=statuses,
        )

    async def select_provider(self, provider: ProviderName) -> AppSettings:
        if not any(model.provider_id == provider for model in await self._repository.list_models()):
            raise ApplicationError(
                "provider_not_available", "The selected provider is not available."
            )
        await self._repository.set_selected_provider(provider)
        return await self.get()

    async def select_model(self, model_id: ModelId) -> AppSettings:
        if not await self._repository.set_selected_model(model_id):
            raise ApplicationError("model_not_available", "The selected model is not available.")
        return await self.get()

    async def save_api_key(self, provider: ProviderName, api_key: str) -> AppSettings:
        if not any(model.provider_id == provider for model in await self._repository.list_models()):
            raise ApplicationError(
                "provider_not_available", "The selected provider is not available."
            )
        if not api_key or len(api_key) > 10_000:
            raise ApplicationError(
                "invalid_api_key",
                "API key must contain 1 to 10,000 characters",
            )
        await self._secret_store.set(provider, api_key)
        return await self.get()

    async def remove_api_key(self, provider: ProviderName) -> AppSettings:
        if not any(model.provider_id == provider for model in await self._repository.list_models()):
            raise ApplicationError(
                "provider_not_available", "The selected provider is not available."
            )
        await self._secret_store.delete(provider)
        return await self.get()
