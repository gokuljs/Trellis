from fastapi import APIRouter
from pydantic import BaseModel, SecretStr

from app.api.dependencies import SettingsServiceDep
from app.domain.models import AppSettings, ProviderName


class ProviderStatus(BaseModel):
    id: ProviderName
    name: str
    model: str
    configured: bool
    key_hint: str | None


class SettingsResponse(BaseModel):
    selected_provider: ProviderName
    providers: list[ProviderStatus]


class ProviderSelection(BaseModel):
    provider: ProviderName


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


@router.put("/providers/{provider}/api-key")
async def update_api_key(
    provider: ProviderName,
    payload: ApiKeyUpdate,
    service: SettingsServiceDep,
) -> SettingsResponse:
    api_key = payload.api_key.get_secret_value()
    return serialize_settings(await service.save_api_key(provider, api_key))


@router.delete("/providers/{provider}/api-key")
async def delete_api_key(
    provider: ProviderName,
    service: SettingsServiceDep,
) -> SettingsResponse:
    return serialize_settings(await service.remove_api_key(provider))
