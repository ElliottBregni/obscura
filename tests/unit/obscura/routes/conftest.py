"""Shared fixtures for route tests.

Auth is always on. Every route test must authenticate, so this conftest
registers a single high-privilege API key via ``OBSCURA_API_KEYS`` and
exposes it as ``TEST_API_KEY`` plus a ``TEST_AUTH_HEADERS`` dict that
test files wire into their ``TestClient`` constructors.
"""

from __future__ import annotations

import pytest

# A single test key that holds every role route tests might need. Routes
# that enforce role gates still exercise their ``require_role`` logic,
# because the dep checks membership — this key just isn't the blocker.
TEST_API_KEY = "test-api-key"
TEST_USER = "test-user"
_TEST_ROLES = ",".join(
    (
        "admin",
        "operator",
        "agent:read",
        "agent:claude",
        "agent:copilot",
        "agent:openai",
        "agent:codex",
        "agent:moonshot",
        "agent:localllm",
        "sync:write",
        "sessions:manage",
        "a2a:invoke",
        "a2a:manage",
        "tier:privileged",
    ),
)

TEST_AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


@pytest.fixture(autouse=True)
def _load_test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the test API key for the duration of each route test."""
    monkeypatch.setenv(
        "OBSCURA_API_KEYS",
        f"{TEST_API_KEY}:{TEST_USER}:{_TEST_ROLES}",
    )
    # Force rbac to re-read env on next access.
    from obscura.auth import rbac

    rbac._load_api_keys()  # noqa: SLF001
