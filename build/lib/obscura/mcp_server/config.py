"""Configuration for the Obscura FastMCP proxy server."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class ObscuraMCPServerConfig(BaseModel):
    """Configuration for the Obscura FastMCP proxy server."""

    model_config = {"extra": "forbid"}

    base_url: str = Field(
        default="http://localhost:8080",
        description="Base URL of the running Obscura FastAPI server",
    )
    api_key: str | None = Field(
        default=None,
        description="API key for authenticating with the Obscura server",
    )
    timeout: float = Field(
        default=60.0,
        description="HTTP request timeout in seconds",
    )

    @classmethod
    def from_env(cls) -> ObscuraMCPServerConfig:
        """Load configuration from environment variables.

        Reads ``OBSCURA_API_KEY`` first.  When that is absent, falls back to
        extracting the first token from the server-side
        ``OBSCURA_API_KEYS`` variable so that in-process MCP tools can
        authenticate against the co-located FastAPI server without
        requiring a separate client key.
        """
        api_key = os.environ.get("OBSCURA_API_KEY")
        if not api_key:
            # OBSCURA_API_KEYS format: "token:user:role1,role2;token2:..."
            keys_env = os.environ.get("OBSCURA_API_KEYS", "")
            if keys_env:
                first_entry = keys_env.split(";")[0]
                api_key = first_entry.split(":")[0] if first_entry else None
        return cls(
            base_url=os.environ.get("OBSCURA_BASE_URL", "http://localhost:8080"),
            api_key=api_key,
            timeout=float(os.environ.get("OBSCURA_MCP_TIMEOUT", "60")),
        )
