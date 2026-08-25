from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, SecretStr

from app.api.dependencies import DatabaseDep, SecretStoreDep
from app.domain.models import ProviderName

PROVIDERS: dict[ProviderName, tuple[str, str]] = {
    "openai": ("OpenAI", "gpt-5.5"),
    "anthropic": ("Anthropic", "claude-sonnet-5"),
}


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


async def build_settings(database: DatabaseDep, secret_store: SecretStoreDep) -> SettingsResponse:
    statuses: list[ProviderStatus] = []
    for provider, (name, model) in PROVIDERS.items():
        configured, key_hint = await secret_store.status(provider)
        statuses.append(
            ProviderStatus(
                id=provider,
                name=name,
                model=model,
                configured=configured,
                key_hint=key_hint,
            )
        )
    return SettingsResponse(
        selected_provider=await database.get_selected_provider(),
        providers=statuses,
    )


@router.get("")
async def get_settings(database: DatabaseDep, secret_store: SecretStoreDep) -> SettingsResponse:
    return await build_settings(database, secret_store)


@router.put("/provider")
async def select_provider(
    payload: ProviderSelection,
    database: DatabaseDep,
    secret_store: SecretStoreDep,
) -> SettingsResponse:
    await database.set_selected_provider(payload.provider)
    return await build_settings(database, secret_store)


@router.put("/providers/{provider}/api-key")
async def update_api_key(
    provider: ProviderName,
    payload: ApiKeyUpdate,
    database: DatabaseDep,
    secret_store: SecretStoreDep,
) -> SettingsResponse:
    api_key = payload.api_key.get_secret_value()
    if not api_key or len(api_key) > 10_000:
        raise HTTPException(status_code=422, detail="API key must contain 1 to 10,000 characters")
    await secret_store.set(provider, api_key)
    return await build_settings(database, secret_store)


@router.delete("/providers/{provider}/api-key")
async def delete_api_key(
    provider: ProviderName,
    database: DatabaseDep,
    secret_store: SecretStoreDep,
) -> SettingsResponse:
    await secret_store.delete(provider)
    return await build_settings(database, secret_store)
