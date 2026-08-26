from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.application.chat import ChatService
from app.application.errors import ApplicationError
from app.application.ports import ProviderAdapter
from app.application.profile import ProfileService
from app.application.sessions import SessionService
from app.application.settings import SettingsService
from app.core.config import Settings
from app.domain.models import ProviderName
from app.infrastructure.database import Database
from app.infrastructure.providers import AnthropicProvider, OpenAIProvider
from app.infrastructure.secrets import SecretStore

ERROR_STATUS = {
    "session_not_found": 404,
    "provider_not_configured": 409,
    "provider_not_available": 503,
    "provider_invalid_response": 502,
    "provider_auth_failed": 502,
    "provider_rate_limited": 429,
    "provider_timeout": 504,
    "provider_upstream_failed": 502,
    "turn_conflict": 409,
    "turn_in_progress": 409,
    "message_empty": 422,
    "invalid_api_key": 422,
}


def create_app(
    settings: Settings | None = None,
    provider_adapters: Mapping[ProviderName, ProviderAdapter] | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_path)
        await database.initialize()
        application.state.database = database
        secret_store = SecretStore(resolved_settings.secrets_path)
        application.state.secret_store = secret_store
        application.state.profile_service = ProfileService(database)
        application.state.session_service = SessionService(database)
        application.state.settings_service = SettingsService(database, secret_store)
        timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            if provider_adapters is None:
                providers: Mapping[ProviderName, ProviderAdapter] = {
                    "openai": OpenAIProvider(http_client),
                    "anthropic": AnthropicProvider(http_client),
                }
            else:
                providers = provider_adapters
            application.state.chat_service = ChatService(
                database,
                secret_store,
                providers,
            )
            yield

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
