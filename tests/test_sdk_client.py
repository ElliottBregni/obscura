"""Tests for sdk.client — ObscuraClient with mocked backends."""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sdk._types import (
    Backend,
    ChunkKind,
    ContentBlock,
    HookPoint,
    Message,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)
from sdk.client import ObscuraClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_copilot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars so auth resolution doesn't fail for Copilot."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")


@pytest.fixture()
def mock_claude_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars so auth resolution doesn't fail for Claude."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

class TestModelResolution:
    def test_copilot_alias_resolves(self, mock_copilot_env: None) -> None:
        """Model alias should be resolved via copilot_models.resolve()."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(
            Backend.COPILOT, model=None, model_alias="copilot_automation_safe",
            automation_safe=False,
        )
        assert model == "gpt-5-mini"

    def test_copilot_alias_automation_safe(self, mock_copilot_env: None) -> None:
        """Automation-safe flag should use require_automation_safe()."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(
            Backend.COPILOT, model=None, model_alias="copilot_automation_safe",
            automation_safe=True,
        )
        assert model == "gpt-5-mini"

    def test_copilot_premium_blocked_by_automation_safe(self, mock_copilot_env: None) -> None:
        """Premium alias should be rejected when automation_safe=True."""
        client = ObscuraClient.__new__(ObscuraClient)
        with pytest.raises(ValueError, match="NOT safe for automation"):
            client._resolve_model(
                Backend.COPILOT, model=None,
                model_alias="copilot_premium_manual_only",
                automation_safe=True,
            )

    def test_raw_model_passes_through(self, mock_copilot_env: None) -> None:
        """Raw model ID should pass through unchanged."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(
            Backend.COPILOT, model="gpt-5", model_alias=None, automation_safe=False,
        )
        assert model == "gpt-5"

    def test_claude_alias_becomes_model(self, mock_claude_env: None) -> None:
        """For Claude, model_alias falls back to being the model ID."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(
            Backend.CLAUDE, model=None, model_alias="claude-sonnet-4-5-20250929",
            automation_safe=False,
        )
        assert model == "claude-sonnet-4-5-20250929"


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

class TestBackendSelection:
    def test_copilot_backend_created(self, mock_copilot_env: None) -> None:
        """Backend.COPILOT should create a CopilotBackend."""
        client = ObscuraClient("copilot", model="gpt-5-mini")
        from sdk.copilot_backend import CopilotBackend
        assert isinstance(client.backend_impl, CopilotBackend)
        assert client.backend_type is Backend.COPILOT

    def test_claude_backend_created(self, mock_claude_env: None) -> None:
        """Backend.CLAUDE should create a ClaudeBackend."""
        client = ObscuraClient("claude")
        from sdk.claude_backend import ClaudeBackend
        assert isinstance(client.backend_impl, ClaudeBackend)
        assert client.backend_type is Backend.CLAUDE

    def test_string_backend(self, mock_copilot_env: None) -> None:
        """String 'copilot' should be converted to Backend.COPILOT."""
        client = ObscuraClient("copilot", model="gpt-5-mini")
        assert client.backend_type is Backend.COPILOT

    def test_invalid_backend(self) -> None:
        """Invalid backend string should raise ValueError."""
        with pytest.raises(ValueError):
            ObscuraClient("invalid_backend")


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------

class TestAuthResolution:
    def test_missing_copilot_auth_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing GitHub token should raise ValueError."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)

        # Mock gh CLI not found
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(ValueError, match="Copilot auth requires"):
                ObscuraClient("copilot", model="gpt-5-mini")

    def test_missing_claude_auth_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing Anthropic API key should raise ValueError."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="Claude auth requires"):
            ObscuraClient("claude")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_tools_passed_at_init(self, mock_copilot_env: None) -> None:
        """Tools passed at init should be registered with the backend."""
        spec = ToolSpec(
            name="test_tool",
            description="A test",
            parameters={},
            handler=lambda: None,
        )
        client = ObscuraClient("copilot", model="gpt-5-mini", tools=[spec])
        # Tool should be in the backend's tool list
        assert len(client.backend_impl._tools) == 1
        assert client.backend_impl._tools[0].name == "test_tool"

    def test_register_tool_after_init(self, mock_copilot_env: None) -> None:
        """register_tool() should add to both registry and backend."""
        client = ObscuraClient("copilot", model="gpt-5-mini")
        spec = ToolSpec(
            name="late_tool",
            description="Added later",
            parameters={},
            handler=lambda: None,
        )
        client.register_tool(spec)
        assert "late_tool" in client._tool_registry
        assert len(client.backend_impl._tools) == 1


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------

class TestHookRegistration:
    def test_register_hook(self, mock_copilot_env: None) -> None:
        """on() should register a hook with the backend."""
        client = ObscuraClient("copilot", model="gpt-5-mini")

        callback = lambda ctx: None  # noqa: E731
        client.on(HookPoint.PRE_TOOL_USE, callback)

        assert callback in client.backend_impl._hooks[HookPoint.PRE_TOOL_USE]
