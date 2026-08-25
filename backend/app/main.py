from fastapi import FastAPI

from app.api import router
from app.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    application = FastAPI(
        title=resolved_settings.app_name,
        debug=resolved_settings.debug,
        version="0.1.0",
    )
    application.include_router(router)
    return application


app = create_app()

__all__ = ["app", "create_app"]
