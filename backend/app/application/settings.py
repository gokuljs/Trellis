from app.application.budgets import BudgetPreset
from app.application.errors import ApplicationError
from app.application.model_catalog import profile_for_model
from app.application.ports import SecretStorePort, SettingsRepository
from app.domain.capabilities import RuntimeCapabilities
from app.domain.models import (
    AppSettings,
    ModelDescriptor,
    ModelId,
    ModelStatus,
    ProviderName,
    ProviderStatus,
)


class SettingsService:
    def __init__(
        self,
        repository: SettingsRepository,
        secret_store: SecretStorePort,
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> None:
        self._repository = repository
        self._secret_store = secret_store
        self._capabilities = capabilities or RuntimeCapabilities()

    async def get(self) -> AppSettings:
        descriptors = await self._repository.list_catalog_models()
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
                        key_hint=None,
                    )
                )
            profile = profile_for_model(model.id)
            reasons = pending_reasons(model)
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
                    key_hint=None,
                    enabled=model.enabled,
                    selectable=is_selectable(model),
                    context_window_tokens=profile.context_window_tokens if profile else None,
                    max_input_tokens=profile.max_input_tokens if profile else None,
                    max_output_tokens=profile.max_output_tokens if profile else None,
                    supports_native_compaction=profile.supports_native_compaction
                    if profile
                    else None,
                    metadata_status="pending" if reasons else "ready",
                    pending_reasons=reasons
                    or (
                        ()
                        if model.enabled
                        else (
                            "provider_unavailable"
                            if model.availability == "unavailable"
                            else "availability_unverified",
                        )
                    ),
                    verification_sources=profile.sources if profile else (),
                    metadata_verified_at=model.metadata_verified_at,
                    availability=model.availability,
                    availability_checked_at=model.availability_checked_at,
                )
            )
        return AppSettings(
            selected_provider=await self._repository.get_selected_provider(),
            providers=providers,
            selected_model_id=await self._repository.get_selected_model_id(),
            models=statuses,
            default_budget_preset=await self._repository.get_default_budget_preset(),
            capabilities=tuple(sorted(self._capabilities.enabled)),
        )

    async def select_default_budget(self, preset: BudgetPreset) -> AppSettings:
        await self._repository.set_default_budget_preset(preset)
        return await self.get()

    async def select_provider(self, provider: ProviderName) -> AppSettings:
        if not any(
            model.provider_id == provider and is_selectable(model)
            for model in await self._repository.list_models()
        ):
            raise ApplicationError(
                "provider_not_available", "The selected provider is not available."
            )
        await self._repository.set_selected_provider(provider)
        return await self.get()

    async def select_model(self, model_id: ModelId) -> AppSettings:
        model = next(
            (item for item in await self._repository.list_models() if item.id == model_id), None
        )
        if model is None or not is_selectable(model):
            raise ApplicationError("model_not_available", "The selected model is not available.")
        if not await self._repository.set_selected_model(model_id):
            raise ApplicationError("model_not_available", "The selected model is not available.")
        return await self.get()


def pending_reasons(model: ModelDescriptor) -> tuple[str, ...]:
    profile = profile_for_model(model.id)
    if (
        profile is None
        or profile.provider_id != model.provider_id
        or profile.upstream_model_id != model.upstream_model_id
        or model.adapter_kind != model.provider_id
    ):
        return ("metadata_unverified",)
    if model.metadata_status == "pending":
        return model.pending_reasons or ("metadata_unverified",)
    return profile.pending_reasons


def is_selectable(model: ModelDescriptor) -> bool:
    return (
        model.enabled
        and not pending_reasons(model)
        and model.supports_streaming
        and model.supports_tools
        and model.availability != "unavailable"
    )
