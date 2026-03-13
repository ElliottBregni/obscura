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
        """Load configuration from environment variables."""
        return cls(
            base_url=os.environ.get("OBSCURA_BASE_URL", "http://localhost:8080"),
            api_key=os.environ.get("OBSCURA_API_KEY"),
            timeout=float(os.environ.get("OBSCURA_MCP_TIMEOUT", "60")),
        )
