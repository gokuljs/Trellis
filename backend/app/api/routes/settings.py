from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import CurrentUserDep, SettingsServiceDep
from app.api.errors import ApiErrorResponse
from app.application.budgets import BudgetPreset
from app.application.errors import ApplicationError
from app.domain.capabilities import RuntimeCapability
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

    enabled: bool = True
    selectable: bool = False
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_native_compaction: bool | None = None
    metadata_status: Literal["ready", "pending"] = "pending"
    pending_reasons: tuple[str, ...] = ()
    verification_sources: tuple[str, ...] = ()
    metadata_verified_at: str | None = None
    availability: Literal["available", "unavailable", "unknown"] = "unknown"
    availability_checked_at: str | None = None


class SettingsResponse(BaseModel):
    selected_provider: ProviderName
    providers: list[ProviderStatus]
    selected_model_id: ModelId
    models: list[ModelOption]
    default_budget_preset: BudgetPreset
    capabilities: list[RuntimeCapability]
    research_available: bool = False
    research_unavailable_reason: str | None = None


class ProviderSelection(BaseModel):
    provider: ProviderName


class ModelSelection(BaseModel):
    model_id: ModelId


class BudgetSelection(BaseModel):
    budget_preset: BudgetPreset


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
                key_hint=None,
            )
            for provider in settings.providers
        ],
        selected_model_id=settings.selected_model_id,
        default_budget_preset=settings.default_budget_preset,
        capabilities=list(settings.capabilities),
        research_available=settings.research_available,
        research_unavailable_reason=settings.research_unavailable_reason,
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
                key_hint=None,
                enabled=model.enabled,
                selectable=model.selectable,
                context_window_tokens=model.context_window_tokens,
                max_input_tokens=model.max_input_tokens,
                max_output_tokens=model.max_output_tokens,
                supports_native_compaction=model.supports_native_compaction,
                metadata_status=model.metadata_status,
                pending_reasons=model.pending_reasons,
                verification_sources=model.verification_sources,
                metadata_verified_at=model.metadata_verified_at,
                availability=model.availability,
                availability_checked_at=model.availability_checked_at,
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


@router.put(
    "/providers/{provider}/api-key",
    status_code=403,
    response_model=ApiErrorResponse,
    deprecated=True,
)
async def update_api_key(
    provider: str,
    _user: CurrentUserDep,
) -> None:
    raise ApplicationError(
        "provider_credentials_managed",
        "Provider access is managed by the Trellis operator.",
    )


@router.delete(
    "/providers/{provider}/api-key",
    status_code=403,
    response_model=ApiErrorResponse,
    deprecated=True,
)
async def delete_api_key(
    provider: str,
    _user: CurrentUserDep,
) -> None:
    raise ApplicationError(
        "provider_credentials_managed",
        "Provider access is managed by the Trellis operator.",
    )
