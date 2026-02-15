"""
sdk._auth — Authentication resolution for both backends.

Resolves credentials from explicit config, environment variables, or
CLI-based fallbacks (``gh auth token`` for Copilot).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from pydantic import BaseModel, ConfigDict

from sdk.internal.types import Backend


# ---------------------------------------------------------------------------
# Auth configuration
# ---------------------------------------------------------------------------

class AuthConfig(BaseModel):
    """Authentication credentials for one or both backends.

    Pass explicit values or leave as None to resolve from environment.
    """
    model_config = ConfigDict(frozen=True)

    # Copilot
    github_token: str | None = None
    # Claude
    anthropic_api_key: str | None = None
    # Copilot BYOK (Bring Your Own Key)
    byok_provider: dict[str, Any] | None = None
    # OpenAI-compatible (OpenAI, OpenRouter, Together, etc.)
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    # Local LLM (LM Studio, Ollama, etc.) — no key needed
    localllm_base_url: str | None = None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_COPILOT_ENV_VARS = ("COPILOT_API_KEY", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
_CLAUDE_ENV_VARS = ("ANTHROPIC_API_KEY",)
_OPENAI_KEY_ENV_VARS = ("OPENAI_API_KEY",)
_OPENAI_BASE_URL_ENV_VARS = ("OPENAI_BASE_URL", "OPENAI_API_BASE")
_LOCALLLM_BASE_URL_ENV_VARS = ("LOCALLLM_BASE_URL", "LM_STUDIO_URL", "OLLAMA_URL")


def _resolve_github_token(explicit: str | None) -> str:
    """Resolve a GitHub token from explicit value, env vars, or gh CLI."""
    if explicit:
        return explicit

    for var in _COPILOT_ENV_VARS:
        token = os.environ.get(var)
        if token:
            return token

    # Fallback: gh auth token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    raise ValueError(
        "Copilot auth requires one of: "
        f"{', '.join(_COPILOT_ENV_VARS)} env var, or `gh auth login`."
    )


def _resolve_anthropic_key(explicit: str | None) -> str:
    """Resolve an Anthropic API key from explicit value or env var."""
    if explicit:
        return explicit

    for var in _CLAUDE_ENV_VARS:
        key = os.environ.get(var)
        if key:
            return key

    raise ValueError(
        "Claude auth requires ANTHROPIC_API_KEY env var."
    )


def _resolve_openai_key(explicit: str | None) -> str:
    """Resolve an OpenAI API key from explicit value or env var."""
    if explicit:
        return explicit

    for var in _OPENAI_KEY_ENV_VARS:
        key = os.environ.get(var)
        if key:
            return key

    raise ValueError(
        "OpenAI auth requires OPENAI_API_KEY env var."
    )


def _resolve_openai_base_url(explicit: str | None) -> str | None:
    """Resolve an OpenAI base URL (optional — defaults to OpenAI's API)."""
    if explicit:
        return explicit

    for var in _OPENAI_BASE_URL_ENV_VARS:
        url = os.environ.get(var)
        if url:
            return url

    return None  # defaults to https://api.openai.com/v1


def _resolve_localllm_base_url(explicit: str | None) -> str:
    """Resolve the local LLM base URL from explicit value or env var."""
    if explicit:
        return explicit

    for var in _LOCALLLM_BASE_URL_ENV_VARS:
        url = os.environ.get(var)
        if url:
            return url

    return "http://localhost:1234/v1"  # LM Studio default


# Public helpers for testing/observability
def resolve_github_token(explicit: str | None) -> str:
    return _resolve_github_token(explicit)


def resolve_anthropic_key(explicit: str | None) -> str:
    return _resolve_anthropic_key(explicit)


def resolve_openai_key(explicit: str | None) -> str:
    return _resolve_openai_key(explicit)


def resolve_openai_base_url(explicit: str | None) -> str | None:
    return _resolve_openai_base_url(explicit)


def resolve_localllm_base_url(explicit: str | None) -> str:
    return _resolve_localllm_base_url(explicit)


class TokenRefresher:
    """Auto-refreshing auth for long-running agents.

    Wraps :func:`resolve_auth` and re-resolves credentials when the
    refresh interval elapses.  Safe for concurrent use — multiple
    callers will see the same cached result until it expires.

    Usage::

        refresher = TokenRefresher(Backend.COPILOT, refresh_interval=3600)
        auth = await refresher.get_valid_auth()
    """

    def __init__(
        self,
        backend: Backend,
        *,
        explicit: AuthConfig | None = None,
        refresh_interval: float = 3600,
    ) -> None:
        self._backend = backend
        self._explicit = explicit
        self._refresh_interval = refresh_interval
        self._cached: AuthConfig | None = None
        self._resolved_at: float = 0.0

    async def get_valid_auth(self) -> AuthConfig:
        """Return cached auth or re-resolve if interval has elapsed."""
        import asyncio
        import time

        now = time.monotonic()
        if self._cached is not None and (now - self._resolved_at) < self._refresh_interval:
            return self._cached

        # Re-resolve in a thread to avoid blocking the event loop
        self._cached = await asyncio.to_thread(
            resolve_auth, self._backend, self._explicit,
        )
        self._resolved_at = time.monotonic()
        return self._cached

    def invalidate(self) -> None:
        """Force re-resolution on next call."""
        self._cached = None
        self._resolved_at = 0.0

    @property
    def cached_auth(self) -> AuthConfig | None:
        """Read-only access to cached auth (for testing/observability)."""
        return self._cached


def resolve_auth(
    backend: Backend,
    explicit: AuthConfig | None = None,
    user: object | None = None,
) -> AuthConfig:
    """Resolve auth credentials for a backend.

    Priority: explicit AuthConfig values > environment variables > CLI fallback.
    Raises ValueError with guidance when credentials cannot be found.

    Parameters
    ----------
    backend:
        Which backend to resolve credentials for.
    explicit:
        Caller-provided credentials (takes priority over env vars).
    user:
        Optional :class:`~sdk.auth.models.AuthenticatedUser` from the
        HTTP server.  When provided, future per-user credential scoping
        can use the identity to select organisation-specific secrets.
        Currently unused but wired through so that the server can pass
        the authenticated user context into backend auth resolution.
    """
    config = explicit or AuthConfig()

    if backend == Backend.COPILOT:
        # BYOK mode skips GitHub auth entirely
        if config.byok_provider is not None:
            return config
        token = _resolve_github_token(config.github_token)
        return AuthConfig(
            github_token=token,
            byok_provider=config.byok_provider,
        )

    if backend == Backend.CLAUDE:
        key = _resolve_anthropic_key(config.anthropic_api_key)
        return AuthConfig(anthropic_api_key=key)

    if backend == Backend.OPENAI:
        key = _resolve_openai_key(config.openai_api_key)
        base_url = _resolve_openai_base_url(config.openai_base_url)
        return AuthConfig(openai_api_key=key, openai_base_url=base_url)

    if backend == Backend.LOCALLLM:
        base_url = _resolve_localllm_base_url(config.localllm_base_url)
        return AuthConfig(localllm_base_url=base_url)

    raise ValueError(f"Unknown backend: {backend}")
