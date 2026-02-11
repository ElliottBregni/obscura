"""Routes: health / readiness probes."""

from fastapi import APIRouter

from sdk.schemas import HealthResponse

router = APIRouter(tags=["infra"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe -- always returns 200."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    """Readiness probe -- returns 200 when the server can serve traffic."""
    return HealthResponse(status="ok")
