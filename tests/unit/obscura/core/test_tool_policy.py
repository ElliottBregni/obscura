"""Tests for obscura.core.tool_policy — Unified tool restriction."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from obscura.core.tool_policy import ToolPolicy
from obscura.core.types import ToolSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_tools() -> list[ToolSpec]:
    """Sample tools for testing."""
    def dummy_handler() -> None:
        pass
    
    return [
        ToolSpec(
            name="search",
            description="Search tool",
            parameters={"type": "object"},
            handler=dummy_handler,
        ),
        ToolSpec(
            name="fetch",
            description="Fetch tool", 
            parameters={"type": "object"},
            handler=dummy_handler,
        ),
        ToolSpec(
            name="execute",
            description="Execute tool",
            parameters={"type": "object"},
            handler=dummy_handler,
        ),
    ]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove OBSCURA_ALLOW_NATIVE_TOOLS from environment."""
    monkeypatch.delenv("OBSCURA_ALLOW_NATIVE_TOOLS", raising=False)


# ---------------------------------------------------------------------------
# Factory Methods Tests
# ---------------------------------------------------------------------------


class TestToolPolicyFactories:
    """Test factory method constructors."""
    
    def test_custom_only(self) -> None:
        """custom_only() creates policy blocking native tools."""
        policy = ToolPolicy.custom_only()
        assert policy.allow_native is False
        assert policy.allowed_tools is None
        assert policy.denied_tools is None
    
    def test_allow_all(self) -> None:
        """allow_all() creates policy allowing native tools."""
        policy = ToolPolicy.allow_all()
        assert policy.allow_native is True
        assert policy.allowed_tools is None
        assert policy.denied_tools is None
    
    def test_restricted(self) -> None:
        """restricted() creates policy with specific allowed tools."""
        policy = ToolPolicy.restricted(["search", "fetch"])
        assert policy.allow_native is False
        assert policy.allowed_tools == ["search", "fetch"]
        assert policy.denied_tools is None
    
    def test_blocked(self) -> None:
        """blocked() creates policy with specific denied tools."""
        policy = ToolPolicy.blocked(["execute", "dangerous"])
        assert policy.allow_native is True
        assert policy.allowed_tools is None
        assert policy.denied_tools == ["execute", "dangerous"]
    
    def test_from_env_default(self, clean_env: None) -> None:
        """from_env() defaults to blocking native tools when no env var."""
        policy = ToolPolicy.from_env()
        assert policy.allow_native is False
    
    def test_from_env_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() allows native when OBSCURA_ALLOW_NATIVE_TOOLS=true."""
        monkeypatch.setenv("OBSCURA_ALLOW_NATIVE_TOOLS", "true")
        policy = ToolPolicy.from_env()
        assert policy.allow_native is True
    
    def test_from_env_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() blocks native when OBSCURA_ALLOW_NATIVE_TOOLS=false."""
        monkeypatch.setenv("OBSCURA_ALLOW_NATIVE_TOOLS", "false")
        policy = ToolPolicy.from_env()
        assert policy.allow_native is False
    
    def test_from_env_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """from_env() is case-insensitive."""
        monkeypatch.setenv("OBSCURA_ALLOW_NATIVE_TOOLS", "TRUE")
        policy = ToolPolicy.from_env()
        assert policy.allow_native is True
        
        monkeypatch.setenv("OBSCURA_ALLOW_NATIVE_TOOLS", "False")
        policy = ToolPolicy.from_env()
        assert policy.allow_native is False
    
    def test_from_env_numeric(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() handles numeric values."""
        monkeypatch.setenv("OBSCURA_ALLOW_NATIVE_TOOLS", "1")
        policy = ToolPolicy.from_env()
        assert policy.allow_native is True
        
        monkeypatch.setenv("OBSCURA_ALLOW_NATIVE_TOOLS", "0")
        policy = ToolPolicy.from_env()
        assert policy.allow_native is False
    
    def test_from_env_yes_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() handles yes/no values."""
        monkeypatch.setenv("OBSCURA_ALLOW_NATIVE_TOOLS", "yes")
        policy = ToolPolicy.from_env()
        assert policy.allow_native is True
        
        monkeypatch.setenv("OBSCURA_ALLOW_NATIVE_TOOLS", "no")
        policy = ToolPolicy.from_env()
        assert policy.allow_native is False
    
    def test_from_env_custom_var_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """from_env() accepts custom variable name."""
        monkeypatch.setenv("MY_CUSTOM_VAR", "true")
        policy = ToolPolicy.from_env(var_name="MY_CUSTOM_VAR")
        assert policy.allow_native is True


# ---------------------------------------------------------------------------
# Copilot Backend Integration Tests
# ---------------------------------------------------------------------------


class TestToolPolicyCopilot:
    """Test apply_to_copilot() method."""
    
    def test_apply_blocks_native_with_custom_only(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """custom_only policy adds allowed_tools to Copilot config."""
        policy = ToolPolicy.custom_only()
        config: dict[str, Any] = {}
        
        policy.apply_to_copilot(config, sample_tools)
        
        assert "allowed_tools" in config
        assert config["allowed_tools"] == ["search", "fetch", "execute"]
    
    def test_apply_no_restriction_with_allow_all(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """allow_all policy does not add allowed_tools."""
        policy = ToolPolicy.allow_all()
        config: dict[str, Any] = {}
        
        policy.apply_to_copilot(config, sample_tools)
        
        assert "allowed_tools" not in config
    
    def test_apply_restricted_list(self, sample_tools: list[ToolSpec]) -> None:
        """restricted() policy only allows specified tools."""
        policy = ToolPolicy.restricted(["search", "fetch"])
        config: dict[str, Any] = {}
        
        policy.apply_to_copilot(config, sample_tools)
        
        assert config["allowed_tools"] == ["search", "fetch"]
    
    def test_apply_with_denied_tools(self, sample_tools: list[ToolSpec]) -> None:
        """Denied tools are filtered from allowed list."""
        policy = ToolPolicy(allow_native=False, denied_tools=["execute"])
        config: dict[str, Any] = {}
        
        policy.apply_to_copilot(config, sample_tools)
        
        assert "execute" not in config["allowed_tools"]
        assert "search" in config["allowed_tools"]
        assert "fetch" in config["allowed_tools"]
    
    def test_apply_with_allowed_and_denied(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """Denied tools are removed even from explicit allowed list."""
        policy = ToolPolicy(
            allow_native=False,
            allowed_tools=["search", "fetch", "execute"],
            denied_tools=["execute"],
        )
        config: dict[str, Any] = {}
        
        policy.apply_to_copilot(config, sample_tools)
        
        assert config["allowed_tools"] == ["search", "fetch"]
    
    def test_apply_with_empty_tools(self) -> None:
        """Policy handles empty tools list gracefully."""
        policy = ToolPolicy.custom_only()
        config: dict[str, Any] = {}
        
        policy.apply_to_copilot(config, [])
        
        assert "allowed_tools" not in config
    
    def test_apply_preserves_existing_config(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """Policy doesn't overwrite existing config keys."""
        policy = ToolPolicy.custom_only()
        config: dict[str, Any] = {
            "model": "gpt-4",
            "temperature": 0.7,
        }
        
        policy.apply_to_copilot(config, sample_tools)
        
        assert config["model"] == "gpt-4"
        assert config["temperature"] == 0.7
        assert "allowed_tools" in config


# ---------------------------------------------------------------------------
# Claude Backend Integration Tests
# ---------------------------------------------------------------------------


class TestToolPolicyClaude:
    """Test apply_to_claude() method."""
    
    def test_apply_uses_mcp_naming(self, sample_tools: list[ToolSpec]) -> None:
        """Claude policy uses mcp__obscura_tools__ prefix."""
        policy = ToolPolicy.custom_only()
        opts: dict[str, Any] = {}
        
        policy.apply_to_claude(opts, sample_tools)
        
        assert "allowed_tools" in opts
        expected = [
            "mcp__obscura_tools__search",
            "mcp__obscura_tools__fetch",
            "mcp__obscura_tools__execute",
        ]
        assert opts["allowed_tools"] == expected
    
    def test_apply_restricted_with_mcp_naming(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """restricted() policy applies MCP naming to allowed list."""
        policy = ToolPolicy.restricted(["search"])
        opts: dict[str, Any] = {}
        
        policy.apply_to_claude(opts, sample_tools)
        
        assert opts["allowed_tools"] == ["mcp__obscura_tools__search"]
    
    def test_apply_denied_with_mcp_naming(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """Denied tools are filtered with MCP naming."""
        policy = ToolPolicy(allow_native=False, denied_tools=["execute"])
        opts: dict[str, Any] = {}
        
        policy.apply_to_claude(opts, sample_tools)
        
        assert "mcp__obscura_tools__execute" not in opts["allowed_tools"]
        assert "mcp__obscura_tools__search" in opts["allowed_tools"]
    
    def test_apply_allow_all_no_restriction(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """allow_all policy doesn't add allowed_tools for Claude."""
        policy = ToolPolicy.allow_all()
        opts: dict[str, Any] = {}
        
        policy.apply_to_claude(opts, sample_tools)
        
        assert "allowed_tools" not in opts


# ---------------------------------------------------------------------------
# OpenAI Backend Integration Tests
# ---------------------------------------------------------------------------


class TestToolPolicyOpenAI:
    """Test apply_to_openai() method."""
    
    def test_apply_filters_tools(self, sample_tools: list[ToolSpec]) -> None:
        """OpenAI policy filters tools list."""
        policy = ToolPolicy.restricted(["search", "fetch"])
        config: dict[str, Any] = {}
        
        policy.apply_to_openai(config, sample_tools)
        
        assert "tools" in config
        assert len(config["tools"]) == 2
        tool_names = [t.name for t in config["tools"]]
        assert "search" in tool_names
        assert "fetch" in tool_names
        assert "execute" not in tool_names
    
    def test_apply_denied_tools(self, sample_tools: list[ToolSpec]) -> None:
        """Denied tools are removed from OpenAI config."""
        policy = ToolPolicy(allow_native=True, denied_tools=["execute"])
        config: dict[str, Any] = {}
        
        policy.apply_to_openai(config, sample_tools)
        
        tool_names = [t.name for t in config["tools"]]
        assert "execute" not in tool_names
        assert len(config["tools"]) == 2
    
    def test_apply_empty_tools(self) -> None:
        """OpenAI policy handles empty tools gracefully."""
        policy = ToolPolicy.custom_only()
        config: dict[str, Any] = {}
        
        policy.apply_to_openai(config, [])
        
        assert "tools" not in config


# ---------------------------------------------------------------------------
# Utility Methods Tests
# ---------------------------------------------------------------------------


class TestToolPolicyUtilities:
    """Test utility methods."""
    
    def test_is_tool_allowed_custom_tool(self) -> None:
        """is_tool_allowed() returns True for custom tools by default."""
        policy = ToolPolicy.custom_only()
        assert policy.is_tool_allowed("search", is_native=False) is True
    
    def test_is_tool_allowed_native_blocked(self) -> None:
        """is_tool_allowed() returns False for native tools when blocked."""
        policy = ToolPolicy.custom_only()
        assert policy.is_tool_allowed("native_tool", is_native=True) is False
    
    def test_is_tool_allowed_native_allowed(self) -> None:
        """is_tool_allowed() returns True for native tools when allowed."""
        policy = ToolPolicy.allow_all()
        assert policy.is_tool_allowed("native_tool", is_native=True) is True
    
    def test_is_tool_allowed_in_allowed_list(self) -> None:
        """is_tool_allowed() checks allowed_tools list."""
        policy = ToolPolicy.restricted(["search", "fetch"])
        assert policy.is_tool_allowed("search", is_native=False) is True
        assert policy.is_tool_allowed("execute", is_native=False) is False
    
    def test_is_tool_allowed_in_denied_list(self) -> None:
        """is_tool_allowed() checks denied_tools list."""
        policy = ToolPolicy.blocked(["execute"])
        assert policy.is_tool_allowed("execute", is_native=False) is False
        assert policy.is_tool_allowed("search", is_native=False) is True
    
    def test_filter_tools_by_allowed(self, sample_tools: list[ToolSpec]) -> None:
        """filter_tools() filters by allowed_tools list."""
        policy = ToolPolicy.restricted(["search", "fetch"])
        filtered = policy.filter_tools(sample_tools)
        
        assert len(filtered) == 2
        names = [t.name for t in filtered]
        assert "search" in names
        assert "fetch" in names
        assert "execute" not in names
    
    def test_filter_tools_by_denied(self, sample_tools: list[ToolSpec]) -> None:
        """filter_tools() removes denied tools."""
        policy = ToolPolicy.blocked(["execute"])
        filtered = policy.filter_tools(sample_tools)
        
        assert len(filtered) == 2
        names = [t.name for t in filtered]
        assert "execute" not in names
    
    def test_filter_tools_native_blocked(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """filter_tools() blocks all when include_native=True and blocked."""
        policy = ToolPolicy.custom_only()
        filtered = policy.filter_tools(sample_tools, include_native=True)
        
        assert len(filtered) == 0
    
    def test_filter_tools_native_allowed(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """filter_tools() allows all when include_native=True and allowed."""
        policy = ToolPolicy.allow_all()
        filtered = policy.filter_tools(sample_tools, include_native=True)
        
        assert len(filtered) == 3


# ---------------------------------------------------------------------------
# Repr and String Tests
# ---------------------------------------------------------------------------


class TestToolPolicyRepr:
    """Test string representation."""
    
    def test_repr_custom_only(self) -> None:
        """__repr__ shows native=blocked for custom_only."""
        policy = ToolPolicy.custom_only()
        repr_str = repr(policy)
        assert "native=blocked" in repr_str
    
    def test_repr_allow_all(self) -> None:
        """__repr__ shows native=allowed for allow_all."""
        policy = ToolPolicy.allow_all()
        repr_str = repr(policy)
        assert "native=allowed" in repr_str
    
    def test_repr_with_allowed(self) -> None:
        """__repr__ shows allowed tools."""
        policy = ToolPolicy.restricted(["search", "fetch"])
        repr_str = repr(policy)
        assert "allowed=['search', 'fetch']" in repr_str
    
    def test_repr_with_denied(self) -> None:
        """__repr__ shows denied tools."""
        policy = ToolPolicy.blocked(["execute"])
        repr_str = repr(policy)
        assert "denied=['execute']" in repr_str


# ---------------------------------------------------------------------------
# Edge Cases and Integration Tests
# ---------------------------------------------------------------------------


class TestToolPolicyEdgeCases:
    """Test edge cases and complex scenarios."""
    
    def test_frozen_dataclass(self) -> None:
        """ToolPolicy is immutable (frozen dataclass)."""
        policy = ToolPolicy.custom_only()
        with pytest.raises(AttributeError):
            policy.allow_native = True  # type: ignore[misc]
    
    def test_complex_policy(self, sample_tools: list[ToolSpec]) -> None:
        """Policy with both allowed and denied lists works correctly."""
        policy = ToolPolicy(
            allow_native=False,
            allowed_tools=["search", "fetch", "execute"],
            denied_tools=["execute"],
        )
        
        # Test filtering
        filtered = policy.filter_tools(sample_tools)
        assert len(filtered) == 2
        names = [t.name for t in filtered]
        assert "execute" not in names
        
        # Test is_tool_allowed
        assert policy.is_tool_allowed("search") is True
        assert policy.is_tool_allowed("execute") is False
        assert policy.is_tool_allowed("other") is False
    
    def test_apply_to_all_backends_consistent(
        self, sample_tools: list[ToolSpec]
    ) -> None:
        """Policy applies consistently across all backends."""
        policy = ToolPolicy.custom_only()
        
        copilot_config: dict[str, Any] = {}
        claude_opts: dict[str, Any] = {}
        openai_config: dict[str, Any] = {}
        
        policy.apply_to_copilot(copilot_config, sample_tools)
        policy.apply_to_claude(claude_opts, sample_tools)
        policy.apply_to_openai(openai_config, sample_tools)
        
        # All should restrict tools
        assert "allowed_tools" in copilot_config
        assert "allowed_tools" in claude_opts
        assert "tools" in openai_config
        
        # Verify counts match
        assert len(copilot_config["allowed_tools"]) == 3
        assert len(claude_opts["allowed_tools"]) == 3
        assert len(openai_config["tools"]) == 3
