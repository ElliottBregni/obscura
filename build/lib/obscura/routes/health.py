"""Routes: health / readiness probes."""

from fastapi import APIRouter

from obscura.schemas import HealthResponse

router = APIRouter(tags=["infra"])


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness probe -- always returns 200."""
    return HealthResponse(status="ok")


@router.get("/ready")
async def ready() -> HealthResponse:
    """Readiness probe -- returns 200 when the server can serve traffic."""
    return HealthResponse(status="ok")
