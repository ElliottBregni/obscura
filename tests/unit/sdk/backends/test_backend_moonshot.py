"""Tests for Moonshot backend defaults and OpenAI-compat wiring."""

from __future__ import annotations

import pytest

from sdk.backends.moonshot import MoonshotBackend
from sdk.internal.auth import AuthConfig
from sdk.internal.types import Backend


def test_moonshot_defaults() -> None:
    backend = MoonshotBackend(AuthConfig(moonshot_api_key="msk-test"))
    assert backend.model == "kimi-2.5"
    assert backend.base_url == "https://api.moonshot.ai/v1"


@pytest.mark.asyncio
async def test_moonshot_session_backend_enum() -> None:
    backend = MoonshotBackend(AuthConfig(moonshot_api_key="msk-test"))
    ref = await backend.create_session()
    assert ref.backend is Backend.MOONSHOT
