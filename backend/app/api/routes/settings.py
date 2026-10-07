from fastapi import APIRouter
from pydantic import BaseModel, SecretStr

from app.api.dependencies import SettingsServiceDep
from app.application.budgets import BudgetPreset
from app.domain.models import AppSettings, ModelId, ProviderName


class ProviderStatus(BaseModel):
    id: ProviderName
    name: str
    model: str
    configured: bool
    key_hint: str | None


class ModelOption(BaseModel):
    id: ModelId
    provider_id: ProviderName
    provider_name: str
    adapter_kind: str
    upstream_model_id: str
    name: str
    requires_api_key: bool
    supports_streaming: bool
    supports_tools: bool
    configured: bool
    key_hint: str | None


class SettingsResponse(BaseModel):
    selected_provider: ProviderName
    providers: list[ProviderStatus]
    selected_model_id: ModelId
    models: list[ModelOption]
    default_budget_preset: BudgetPreset


class ProviderSelection(BaseModel):
    provider: ProviderName


class ModelSelection(BaseModel):
    model_id: ModelId


class BudgetSelection(BaseModel):
    budget_preset: BudgetPreset


class ApiKeyUpdate(BaseModel):
    api_key: SecretStr


router = APIRouter(prefix="/api/settings", tags=["settings"])


def serialize_settings(settings: AppSettings) -> SettingsResponse:
    return SettingsResponse(
        selected_provider=settings.selected_provider,
        providers=[
            ProviderStatus(
                id=provider.id,
                name=provider.name,
                model=provider.model,
                configured=provider.configured,
                key_hint=provider.key_hint,
            )
            for provider in settings.providers
        ],
        selected_model_id=settings.selected_model_id,
        default_budget_preset=settings.default_budget_preset,
        models=[
            ModelOption(
                id=model.id,
                provider_id=model.provider_id,
                provider_name=model.provider_name,
                adapter_kind=model.adapter_kind,
                upstream_model_id=model.upstream_model_id,
                name=model.name,
                requires_api_key=model.requires_api_key,
                supports_streaming=model.supports_streaming,
                supports_tools=model.supports_tools,
                configured=model.configured,
                key_hint=model.key_hint,
            )
            for model in settings.models
        ],
    )


@router.get("")
async def get_settings(service: SettingsServiceDep) -> SettingsResponse:
    return serialize_settings(await service.get())


@router.put("/provider")
async def select_provider(
    payload: ProviderSelection,
    service: SettingsServiceDep,
) -> SettingsResponse:
    return serialize_settings(await service.select_provider(payload.provider))


@router.put("/model")
async def select_model(
    payload: ModelSelection,
    service: SettingsServiceDep,
) -> SettingsResponse:
    return serialize_settings(await service.select_model(payload.model_id))


@router.put("/budget")
async def select_default_budget(
    payload: BudgetSelection,
    service: SettingsServiceDep,
) -> SettingsResponse:
    return serialize_settings(await service.select_default_budget(payload.budget_preset))


@router.put("/providers/{provider}/api-key")
async def update_api_key(
    provider: str,
    payload: ApiKeyUpdate,
    service: SettingsServiceDep,
) -> SettingsResponse:
    api_key = payload.api_key.get_secret_value()
    return serialize_settings(await service.save_api_key(provider, api_key))


@router.delete("/providers/{provider}/api-key")
async def delete_api_key(
    provider: str,
    service: SettingsServiceDep,
) -> SettingsResponse:
    return serialize_settings(await service.remove_api_key(provider))
