from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Protocol, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.api.auth import SupabaseAuthVerifier, VerifiedUser
from app.application.errors import ApplicationError
from app.application.ports import ProviderAdapter, StreamingProviderAdapter
from app.application.tools import ToolRegistry
from app.application.workspaces import (
    DEFAULT_WORKSPACE_PICKER_TIMEOUT_SECONDS,
    WorkspacePickerService,
)
from app.core.config import Settings
from app.domain.models import ProviderName
from app.infrastructure.accounts import AccountRegistry, RepositoryFactory
from app.infrastructure.providers import AnthropicProvider, OpenAIProvider
from app.infrastructure.workspace_picker import NativeFolderPicker

WORKSPACE_PICKER_TIMEOUT_SECONDS = DEFAULT_WORKSPACE_PICKER_TIMEOUT_SECONDS


class AuthVerifier(Protocol):
    async def verify_bearer(self, authorization: str | None) -> VerifiedUser: ...
    async def verify_token(self, token: str) -> VerifiedUser: ...


ERROR_STATUS = {
    "model_not_available": 404,
    "session_not_found": 404,
    "run_not_found": 404,
    "run_not_owned": 409,
    "run_lease_lost": 409,
    "reauth_required": 401,
    "cloud_access_denied": 401,
    "database_unavailable": 503,
    "invalid_workspace": 422,
    "workspace_picker_unavailable": 503,
    "workspace_picker_timeout": 504,
    "session_workspace_busy": 409,
    "session_run_busy": 409,
    "session_turn_busy": 409,
    "account_import_in_progress": 409,
    "account_activity_busy": 409,
    "provider_not_configured": 409,
    "pricing_unavailable": 409,
    "invalid_budget_preset": 422,
    "provider_not_available": 503,
    "provider_invalid_response": 502,
    "provider_auth_failed": 502,
    "provider_rate_limited": 429,
    "provider_timeout": 504,
    "provider_upstream_failed": 502,
    "turn_conflict": 409,
    "turn_in_progress": 409,
    "turn_not_owned": 409,
    "account_in_use": 409,
    "message_empty": 422,
    "invalid_api_key": 422,
    "invalid_profile": 422,
    "onboarding_step_out_of_order": 409,
    "onboarding_already_complete": 409,
}


def create_app(
    settings: Settings | None = None,
    provider_adapters: Mapping[ProviderName, ProviderAdapter] | None = None,
    *,
    streaming_provider_adapters: Mapping[str, StreamingProviderAdapter] | None = None,
    tool_registry: ToolRegistry | None = None,
    workspace_picker: WorkspacePickerService | None = None,
    auth_verifier: AuthVerifier | None = None,
    repository_factory: RepositoryFactory | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = resolved_settings
        application.state.workspace_picker = workspace_picker or WorkspacePickerService(
            NativeFolderPicker(),
            timeout_seconds=WORKSPACE_PICKER_TIMEOUT_SECONDS,
        )
        timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            application.state.auth_verifier = auth_verifier or SupabaseAuthVerifier(
                resolved_settings, http_client
            )
            if provider_adapters is None:
                providers: Mapping[ProviderName, ProviderAdapter] = {
                    "openai": OpenAIProvider(http_client),
                    "anthropic": AnthropicProvider(http_client),
                }
            else:
                providers = provider_adapters
            runtime_providers: dict[str, StreamingProviderAdapter] = {}
            for provider_id, provider in providers.items():
                if callable(getattr(provider, "stream", None)):
                    runtime_providers[provider_id] = cast(StreamingProviderAdapter, provider)
            runtime_providers.update(streaming_provider_adapters or {})
            registry = AccountRegistry(
                resolved_settings,
                providers,
                runtime_providers,
                tool_registry_factory=(
                    (lambda _database: tool_registry) if tool_registry is not None else None
                ),
                cloud_client=http_client,
                repository_factory=repository_factory,
            )
            application.state.account_registry = registry
            try:
                yield
            finally:
                await registry.close()

    application = FastAPI(
        title=resolved_settings.app_name,
        debug=resolved_settings.debug,
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.exception_handler(ApplicationError)
    async def handle_application_error(_request: Request, error: ApplicationError) -> JSONResponse:
        return JSONResponse(
            status_code=ERROR_STATUS.get(error.code, 500),
            content={"error": {"code": error.code, "message": error.message}},
        )

    application.include_router(router)
    return application


app = create_app()

__all__ = ["app", "create_app"]
