"""Routes: health / readiness probes."""

from fastapi import APIRouter

from obscura.core.config import ObscuraConfig
from obscura.schemas import HealthResponse

router = APIRouter(tags=["infra"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe -- always returns 200."""
    cfg = ObscuraConfig()
    return HealthResponse(status="ok", auth_enabled=cfg.auth_enabled)


@router.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    """Readiness probe -- returns 200 when the server can serve traffic."""
    return HealthResponse(status="ok")
