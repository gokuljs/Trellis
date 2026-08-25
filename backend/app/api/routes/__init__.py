from fastapi import APIRouter

from app.api.routes.health import router as health_router
from app.api.routes.profile import router as profile_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.settings import router as settings_router

router = APIRouter()
router.include_router(health_router)
router.include_router(profile_router)
router.include_router(settings_router)
router.include_router(sessions_router)

__all__ = ["router"]
