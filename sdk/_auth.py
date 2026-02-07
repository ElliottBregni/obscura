"""
sdk._auth — Authentication resolution for both backends.

Resolves credentials from explicit config, environment variables, or
CLI-based fallbacks (``gh auth token`` for Copilot).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from sdk._types import Backend


# ---------------------------------------------------------------------------
# Auth configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthConfig:
    """Authentication credentials for one or both backends.

    Pass explicit values or leave as None to resolve from environment.
    """
    # Copilot
    github_token: str | None = None
    # Claude
    anthropic_api_key: str | None = None
    # Copilot BYOK (Bring Your Own Key)
    byok_provider: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_COPILOT_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
_CLAUDE_ENV_VARS = ("ANTHROPIC_API_KEY",)


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


def resolve_auth(backend: Backend, explicit: AuthConfig | None = None) -> AuthConfig:
    """Resolve auth credentials for a backend.

    Priority: explicit AuthConfig values > environment variables > CLI fallback.
    Raises ValueError with guidance when credentials cannot be found.
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

    raise ValueError(f"Unknown backend: {backend}")
