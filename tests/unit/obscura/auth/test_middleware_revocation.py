"""Tests that the APIKeyAuthMiddleware enforces revocation + idle timeout.

These use a fake starlette Request/Response so we don't drag in the
full FastAPI server — the middleware's dispatch logic is what matters.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from obscura.auth.middleware import APIKeyAuthMiddleware
from obscura.auth import revocation as revocation_mod
from obscura.auth import session_activity as activity_mod


@dataclass
class _FakeURL:
    path: str


class _FakeState:
    def __init__(self) -> None:
        self.user: Any = None
        self.token_jti: str = ""
        self.session_id: str = ""


class _FakeRequest:
    def __init__(self, path: str, headers: dict[str, str]) -> None:
        self.url = _FakeURL(path=path)
        self.headers = headers
        self.state = _FakeState()


async def _passthrough(_req: Any) -> Any:
    return "ok"


@pytest.fixture(autouse=True)
def _clean_singletons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point the module-level blocklist at a tmp DB so tests don't
    touch ~/.obscura/."""
    monkeypatch.setenv("OBSCURA_REVOCATIONS_DB", str(tmp_path / "r.db"))
    revocation_mod.reset_default_blocklist()
    activity_mod.reset_default_tracker()
    yield
    revocation_mod.reset_default_blocklist()
    activity_mod.reset_default_tracker()


@pytest.fixture
def valid_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv(
        "OBSCURA_API_KEYS",
        # Single role; colons inside a role name break the parser.
        "test-key-1:alice:agent",
    )
    from obscura.auth import rbac as rbac_mod

    rbac_mod._load_api_keys()  # type: ignore[reportPrivateUsage]
    yield "test-key-1"
    # Restore the pre-test key state so sibling tests that rely on the
    # default dev-key see it again.
    monkeypatch.delenv("OBSCURA_API_KEYS", raising=False)
    rbac_mod._load_api_keys()  # type: ignore[reportPrivateUsage]


async def _dispatch(
    mw: APIKeyAuthMiddleware, req: _FakeRequest
) -> Any:
    return await mw.dispatch(req, _passthrough)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_api_path_bypasses_checks() -> None:
    mw = APIKeyAuthMiddleware(app=None)
    req = _FakeRequest("/health", headers={})
    assert await _dispatch(mw, req) == "ok"


@pytest.mark.asyncio
async def test_happy_path_passes_through(valid_key: str) -> None:
    mw = APIKeyAuthMiddleware(app=None)
    req = _FakeRequest("/api/things", headers={"X-API-Key": valid_key})
    assert await _dispatch(mw, req) == "ok"
    assert req.state.user is not None


@pytest.mark.asyncio
async def test_revoked_jti_is_rejected(valid_key: str) -> None:
    revocation_mod.default_blocklist().revoke(
        "jti-bad", expires_at=time.time() + 60
    )
    mw = APIKeyAuthMiddleware(app=None)
    req = _FakeRequest("/api/things", headers={"X-API-Key": valid_key})
    req.state.token_jti = "jti-bad"
    response = await _dispatch(mw, req)
    # starlette JSONResponse exposes status_code attribute.
    assert getattr(response, "status_code", None) == 401


@pytest.mark.asyncio
async def test_unrevoked_jti_passes(valid_key: str) -> None:
    mw = APIKeyAuthMiddleware(app=None)
    req = _FakeRequest("/api/things", headers={"X-API-Key": valid_key})
    req.state.token_jti = "jti-clean"
    assert await _dispatch(mw, req) == "ok"


@pytest.mark.asyncio
async def test_idle_session_is_rejected(valid_key: str) -> None:
    # Warm the tracker with an old last_seen, well past the idle window.
    activity_mod.default_tracker().observe("sess-abc", now=time.time() - 7200)
    mw = APIKeyAuthMiddleware(app=None)
    req = _FakeRequest("/api/things", headers={"X-API-Key": valid_key})
    req.state.session_id = "sess-abc"
    response = await _dispatch(mw, req)
    assert getattr(response, "status_code", None) == 401


@pytest.mark.asyncio
async def test_fresh_session_observed_on_success(valid_key: str) -> None:
    mw = APIKeyAuthMiddleware(app=None)
    req = _FakeRequest("/api/things", headers={"X-API-Key": valid_key})
    req.state.session_id = "sess-new"
    await _dispatch(mw, req)
    tracker = activity_mod.default_tracker()
    # After observe(), the session is known and not idle.
    assert tracker.is_idle("sess-new") is False


@pytest.mark.asyncio
async def test_missing_api_key_rejected() -> None:
    mw = APIKeyAuthMiddleware(app=None)
    req = _FakeRequest("/api/things", headers={})
    response = await _dispatch(mw, req)
    assert getattr(response, "status_code", None) == 401
