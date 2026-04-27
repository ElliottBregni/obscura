"""Integration tests for the X-GitHub-Token FastAPI dep + ClientFactory flow.

Proves the wiring:  request header → ``get_oauth_github_token`` dep →
``ClientFactory.create`` → ``AuthConfig.oauth_github_token``.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from obscura.deps import get_oauth_github_token


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/echo-gh")
    async def echo(
        request: Request,
        token: Annotated[str | None, Depends(get_oauth_github_token)] = None,
    ) -> JSONResponse:
        # Proves the dep resolves the header, not some other source.
        return JSONResponse({"token": token, "path": request.url.path})

    return app


class TestGetOAuthGithubToken:
    @pytest.mark.asyncio
    async def test_returns_none_when_header_absent(self) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_build_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/v1/echo-gh")
            assert resp.status_code == 200
            assert resp.json()["token"] is None

    @pytest.mark.asyncio
    async def test_reads_header(self) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_build_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/echo-gh",
                headers={"X-GitHub-Token": "ghp_fake_token"},
            )
            assert resp.status_code == 200
            assert resp.json()["token"] == "ghp_fake_token"

    @pytest.mark.asyncio
    async def test_header_is_case_insensitive(self) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_build_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/echo-gh",
                headers={"x-github-token": "ghp_fake"},
            )
            assert resp.status_code == 200
            assert resp.json()["token"] == "ghp_fake"

    @pytest.mark.asyncio
    async def test_whitespace_only_treated_as_absent(self) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_build_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/api/v1/echo-gh",
                headers={"X-GitHub-Token": "   "},
            )
            assert resp.status_code == 200
            assert resp.json()["token"] is None


class TestClientFactoryOauthWiring:
    """Prove ``ClientFactory.create`` passes ``oauth_github_token`` into AuthConfig."""

    @pytest.mark.asyncio
    async def test_oauth_token_reaches_authconfig(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The factory must hand the OAuth token to ObscuraClient as AuthConfig."""
        from obscura.auth.copilot_403_cache import clear_cache_for_tests
        from obscura.core.config import ObscuraConfig
        from obscura.deps import ClientFactory

        clear_cache_for_tests()
        captured: dict[str, object] = {}

        class _StubClient:
            def __init__(self, backend: str, **kwargs: object) -> None:
                captured["backend"] = backend
                captured.update(kwargs)

            async def start(self) -> None:
                return None

        monkeypatch.setattr("obscura.deps.ObscuraClient", _StubClient)

        factory = ClientFactory(ObscuraConfig())
        await factory.create("copilot", oauth_github_token="ghp_oauth")

        auth = captured.get("auth")
        assert auth is not None
        # AuthConfig(oauth_github_token=...)
        assert getattr(auth, "oauth_github_token", None) == "ghp_oauth"
        assert getattr(auth, "github_token", None) is None

    @pytest.mark.asyncio
    async def test_no_oauth_token_means_no_authconfig(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth.copilot_403_cache import clear_cache_for_tests
        from obscura.core.config import ObscuraConfig
        from obscura.deps import ClientFactory

        clear_cache_for_tests()
        captured: dict[str, object] = {}

        class _StubClient:
            def __init__(self, backend: str, **kwargs: object) -> None:
                captured.update(kwargs)

            async def start(self) -> None:
                return None

        monkeypatch.setattr("obscura.deps.ObscuraClient", _StubClient)

        factory = ClientFactory(ObscuraConfig())
        await factory.create("copilot")

        # When no OAuth token is supplied, auth should be None so the
        # resolver falls through env/CLI as before.
        assert captured.get("auth") is None

    @pytest.mark.asyncio
    async def test_cached_403_drops_oauth_token(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the token is in the 403 cache, don't pass it to ObscuraClient."""
        from obscura.auth.copilot_403_cache import (
            clear_cache_for_tests,
            mark_oauth_token_blocked,
        )
        from obscura.auth.models import AuthenticatedUser
        from obscura.core.config import ObscuraConfig
        from obscura.deps import ClientFactory

        clear_cache_for_tests()
        user = AuthenticatedUser(
            user_id="user-1",
            email="u@example.com",
            roles=("agent:read",),
            org_id=None,
            token_type="user",
            raw_token="",
        )
        mark_oauth_token_blocked("user-1", "ghp_bad_token")

        captured: dict[str, object] = {}

        class _StubClient:
            def __init__(self, backend: str, **kwargs: object) -> None:
                captured.update(kwargs)

            async def start(self) -> None:
                return None

        monkeypatch.setattr("obscura.deps.ObscuraClient", _StubClient)

        factory = ClientFactory(ObscuraConfig())
        await factory.create(
            "copilot",
            user=user,
            oauth_github_token="ghp_bad_token",
        )

        assert captured.get("auth") is None  # OAuth token was dropped

    @pytest.mark.asyncio
    async def test_copilot_403_triggers_retry_without_oauth(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When Copilot rejects the OAuth token, retry once without it."""
        from obscura.auth.copilot_403_cache import (
            clear_cache_for_tests,
            is_oauth_token_blocked,
        )
        from obscura.auth.models import AuthenticatedUser
        from obscura.core.config import ObscuraConfig
        from obscura.deps import ClientFactory

        clear_cache_for_tests()
        user = AuthenticatedUser(
            user_id="user-2",
            email="u@example.com",
            roles=("agent:read",),
            org_id=None,
            token_type="user",
            raw_token="",
        )

        start_calls: list[object] = []

        class _StubClient:
            def __init__(self, backend: str, **kwargs: object) -> None:
                self.auth = kwargs.get("auth")

            async def start(self) -> None:
                start_calls.append(self.auth)
                if self.auth is not None:
                    raise RuntimeError("Copilot 403 Forbidden")

        monkeypatch.setattr("obscura.deps.ObscuraClient", _StubClient)

        factory = ClientFactory(ObscuraConfig())
        await factory.create(
            "copilot",
            user=user,
            oauth_github_token="ghp_bad",
        )

        assert len(start_calls) == 2  # first with auth, retry without
        assert start_calls[0] is not None
        assert start_calls[1] is None
        assert is_oauth_token_blocked("user-2", "ghp_bad") is True
