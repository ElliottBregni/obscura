"""obscura.auth.rate_limit_middleware — Per-user rate limit enforcement.

Starlette middleware that intercepts requests after authentication,
checks the per-user rate limiter, and returns HTTP 429 with
``Retry-After`` when limits are exceeded.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from obscura.core.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Paths that bypass rate limiting (health checks, public endpoints)
_EXEMPT_PREFIXES = (
    "/api/v1/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/.well-known",
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce per-user rate limits on API requests.

    Must be installed *after* ``APIKeyAuthMiddleware`` in the middleware
    stack so ``request.state.user`` is populated.
    """

    def __init__(self, app: Any, *, limiter: RateLimiter) -> None:
        super().__init__(app)
        self._limiter = limiter

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip exempt paths
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES):
            return await call_next(request)

        # Extract user (set by APIKeyAuthMiddleware)
        user = getattr(request.state, "user", None)
        if user is None:
            # No auth context — let downstream handle it
            return await call_next(request)

        user_id: str = getattr(user, "user_id", "anonymous")

        result = self._limiter.acquire(user_id)
        if not result.allowed:
            self._record_rejection(user_id)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "retry_after": result.retry_after_seconds,
                },
                headers={
                    "Retry-After": str(int(result.retry_after_seconds + 0.5)),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        try:
            response = await call_next(request)
            # Add rate limit headers to successful responses
            response.headers["X-RateLimit-Limit"] = str(result.limit)
            response.headers["X-RateLimit-Remaining"] = str(result.remaining)
            return response
        finally:
            self._limiter.release_concurrent(user_id)

    @staticmethod
    def _record_rejection(user_id: str) -> None:
        """Emit a rate-limit rejection metric."""
        try:
            from obscura.telemetry.metrics import get_metrics

            get_metrics().rate_limit_rejections.add(
                1, {"user_id": user_id}
            )
        except Exception:
            pass
