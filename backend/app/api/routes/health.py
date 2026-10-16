import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", responses={503: {"description": "Service is not ready"}})
async def ready(request: Request) -> JSONResponse:
    available = False
    try:
        async with asyncio.timeout(5):
            available = await request.app.state.readiness_probe.is_ready()
    except Exception:
        pass
    return JSONResponse(
        status_code=200 if available else 503,
        content={"status": "ready" if available else "unavailable"},
        headers={"Cache-Control": "no-store"},
    )
