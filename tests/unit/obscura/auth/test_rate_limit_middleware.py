"""Tests for obscura.auth.rate_limit_middleware."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from obscura.auth.rate_limit_middleware import RateLimitMiddleware
from obscura.core.rate_limiter import RateLimiter


@dataclass
class _FakeUser:
    user_id: str = "test-user"


def _make_app(limiter: RateLimiter) -> Starlette:
    """Build a tiny Starlette app with the rate limit middleware."""

    async def homepage(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "healthy"})

    app = Starlette(
        routes=[
            Route("/api/v1/health", health),
            Route("/api/v1/test", homepage),
            Route("/docs", homepage),
        ],
    )
    # Simulate authenticated user via middleware
    class _InjectUser:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                scope.setdefault("state", {})
            await self.app(scope, receive, send)

    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    return app


def _make_app_with_user(
    limiter: RateLimiter, user: _FakeUser | None = None
) -> Starlette:
    """Build app that injects a user into request.state."""

    async def homepage(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "healthy"})

    app = Starlette(
        routes=[
            Route("/api/v1/health", health),
            Route("/api/v1/test", homepage),
            Route("/docs", homepage),
        ],
    )

    # Inject user into state before rate limit middleware runs
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

    class _UserInjector(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            if user is not None:
                request.state.user = user
            return await call_next(request)

    # Starlette applies middleware in reverse add order (last added = outermost)
    # We want: UserInjector → RateLimitMiddleware → app
    # So add RateLimitMiddleware first, then UserInjector (outer)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(_UserInjector)

    return app


class TestExemptPaths:
    def test_health_bypasses_rate_limit(self) -> None:
        limiter = RateLimiter(default_rpm=0)  # zero = deny all
        app = _make_app_with_user(limiter, _FakeUser())
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_docs_bypasses_rate_limit(self) -> None:
        limiter = RateLimiter(default_rpm=0)
        app = _make_app_with_user(limiter, _FakeUser())
        client = TestClient(app)
        resp = client.get("/docs")
        assert resp.status_code == 200


class TestNoUser:
    def test_unauthenticated_passes_through(self) -> None:
        limiter = RateLimiter(default_rpm=0)
        app = _make_app_with_user(limiter, user=None)
        client = TestClient(app)
        resp = client.get("/api/v1/test")
        assert resp.status_code == 200


class TestRateLimitEnforcement:
    def test_allowed_request_has_headers(self) -> None:
        limiter = RateLimiter(default_rpm=10, default_concurrent=5)
        app = _make_app_with_user(limiter, _FakeUser())
        client = TestClient(app)
        resp = client.get("/api/v1/test")
        assert resp.status_code == 200
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers

    def test_denied_returns_429(self) -> None:
        limiter = RateLimiter(default_rpm=1, default_concurrent=10)
        app = _make_app_with_user(limiter, _FakeUser())
        client = TestClient(app)
        # First request succeeds
        resp1 = client.get("/api/v1/test")
        assert resp1.status_code == 200
        # Second request denied
        resp2 = client.get("/api/v1/test")
        assert resp2.status_code == 429
        assert "Retry-After" in resp2.headers
        body = resp2.json()
        assert body["detail"] == "Rate limit exceeded"
        assert "retry_after" in body

    def test_concurrent_limit(self) -> None:
        limiter = RateLimiter(default_rpm=100, default_concurrent=1)
        app = _make_app_with_user(limiter, _FakeUser())
        client = TestClient(app)
        # First request succeeds and releases concurrent on completion
        resp1 = client.get("/api/v1/test")
        assert resp1.status_code == 200
        # Second should also succeed (concurrent released in finally block)
        resp2 = client.get("/api/v1/test")
        assert resp2.status_code == 200


class TestConcurrentRelease:
    def test_concurrent_released_on_success(self) -> None:
        limiter = RateLimiter(default_rpm=100, default_concurrent=1)
        app = _make_app_with_user(limiter, _FakeUser())
        client = TestClient(app)
        # Sequential requests should all succeed because concurrent is released
        for _ in range(5):
            resp = client.get("/api/v1/test")
            assert resp.status_code == 200
