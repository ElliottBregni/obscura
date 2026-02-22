"""
obscura.backends.moonshot — Moonshot/Kimi backend via OpenAI-compatible API.

Implements Moonshot by reusing ``OpenAIBackend`` with provider-specific
defaults:
- model: ``kimi-2.5``
- base URL: ``https://api.moonshot.ai/v1`` (overridable via auth/env)
"""

from __future__ import annotations

from typing import Any

from obscura.providers.openai import OpenAIBackend
from obscura.core.auth import AuthConfig
from obscura.core.types import Backend


class MoonshotBackend(OpenAIBackend):
    """OpenAI-compatible backend configured for Moonshot/Kimi."""

    DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"
    DEFAULT_MODEL = "kimi-2.5"

    def __init__(
        self,
        auth: AuthConfig,
        *,
        model: str | None = None,
        system_prompt: str = "",
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        key = auth.moonshot_api_key or auth.openai_api_key
        base_url = auth.moonshot_base_url or auth.openai_base_url or self.DEFAULT_BASE_URL
        compat_auth = AuthConfig(openai_api_key=key, openai_base_url=base_url)
        super().__init__(
            compat_auth,
            model=model or self.DEFAULT_MODEL,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            backend_type=Backend.MOONSHOT,
        )
