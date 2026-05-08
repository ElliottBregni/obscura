"""Unit tests for obscura.tools.dynamic_discovery.

Tests cover:
  - DynamicToolDiscovery._infer_category (pure static method)
  - DynamicToolDiscovery._convert_entries
  - DynamicToolDiscovery.discover_popular (with mocked providers)
  - DynamicToolDiscovery.discover_by_category
  - AutoInstallToolProvider.generate_config
  - AutoInstallToolProvider.save_config
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from obscura.tools.dynamic_discovery import (
    AutoInstallToolProvider,
    DynamicToolDiscovery,
    ToolCapability,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _infer_category (static, pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "name", "expected"),
    [
        ("filesystem-mcp", "File System", "filesystem"),
        ("github-mcp", "GitHub", "git"),
        ("postgres-mcp", "PostgreSQL", "database"),
        ("puppeteer-mcp", "Browser Automation", "web"),
        ("slack-mcp", "Slack Integration", "communication"),
        ("gcp-mcp", "Google Cloud Platform", "cloud"),
        ("elastic-search", "Elastic Search", "search"),
        ("anthropic-claude-mcp", "Claude AI", "ai"),
        ("unknown-tool", "Unknown", "general"),
    ],
)
def test_infer_category(slug: str, name: str, expected: str) -> None:
    assert DynamicToolDiscovery._infer_category(slug, name) == expected


# ---------------------------------------------------------------------------
# _convert_entries
# ---------------------------------------------------------------------------


def _make_entry(slug: str, name: str, rank: int) -> MagicMock:
    e = MagicMock()
    e.slug = slug
    e.name = name
    e.rank = rank
    return e


def test_convert_entries_produces_tool_capabilities() -> None:
    entries = [
        _make_entry("github-mcp", "GitHub", 1),
        _make_entry("filesystem-mcp", "File System", 2),
    ]

    caps = DynamicToolDiscovery._convert_entries(entries, "registry")  # type: ignore[arg-type]

    assert len(caps) == 2
    assert isinstance(caps[0], ToolCapability)
    assert caps[0].name == "GitHub"
    assert caps[0].popularity_rank == 1


def test_convert_entries_npm_package_set_for_mcp_slug() -> None:
    entries = [_make_entry("@modelcontextprotocol/server-github", "GitHub", 1)]

    caps = DynamicToolDiscovery._convert_entries(entries, "registry")  # type: ignore[arg-type]

    assert caps[0].npm_package == "@modelcontextprotocol/server-github"


def test_convert_entries_installation_command_uses_npx() -> None:
    entries = [_make_entry("github-mcp", "GitHub", 1)]

    caps = DynamicToolDiscovery._convert_entries(entries, "registry")  # type: ignore[arg-type]

    assert caps[0].installation_command is not None
    assert caps[0].installation_command[0] == "npx"


def test_convert_entries_empty_list_returns_empty() -> None:
    assert DynamicToolDiscovery._convert_entries([], "registry") == []


# ---------------------------------------------------------------------------
# discover_popular (with mocked providers)
# ---------------------------------------------------------------------------


def test_discover_popular_returns_sorted_by_rank() -> None:
    discovery = DynamicToolDiscovery()

    entries = [
        _make_entry("tool-b", "Tool B", 5),
        _make_entry("tool-a", "Tool A", 1),
        _make_entry("tool-c", "Tool C", 3),
    ]

    with patch.object(discovery.registry_provider, "fetch_top", return_value=entries):
        caps = discovery.discover_popular(limit=10)

    assert caps[0].popularity_rank == 1
    assert caps[-1].popularity_rank == 5


def test_discover_popular_respects_limit() -> None:
    discovery = DynamicToolDiscovery()

    entries = [_make_entry(f"tool-{i}", f"Tool {i}", i) for i in range(20)]

    with patch.object(discovery.registry_provider, "fetch_top", return_value=entries):
        caps = discovery.discover_popular(limit=5)

    assert len(caps) <= 5


def test_discover_popular_falls_back_to_community_when_registry_empty() -> None:
    discovery = DynamicToolDiscovery()

    community_entries = [_make_entry("community-tool", "Community", 1)]

    with (
        patch.object(discovery.registry_provider, "fetch_top", return_value=[]),
        patch.object(
            discovery.mcpservers_provider, "fetch_top", return_value=community_entries
        ),
    ):
        caps = discovery.discover_popular(limit=10)

    assert len(caps) == 1
    assert caps[0].name == "Community"


def test_discover_popular_filters_by_min_rank() -> None:
    discovery = DynamicToolDiscovery()

    entries = [
        _make_entry("tool-1", "Tool 1", 1),
        _make_entry("tool-5", "Tool 5", 5),
        _make_entry("tool-10", "Tool 10", 10),
    ]

    with patch.object(discovery.registry_provider, "fetch_top", return_value=entries):
        caps = discovery.discover_popular(limit=10, min_rank=5)

    ranks = [c.popularity_rank for c in caps]
    assert all(r <= 5 for r in ranks)


def test_discover_popular_registry_exception_falls_back() -> None:
    discovery = DynamicToolDiscovery()

    community_entries = [_make_entry("fallback-tool", "Fallback", 1)]

    with (
        patch.object(
            discovery.registry_provider,
            "fetch_top",
            side_effect=RuntimeError("network"),
        ),
        patch.object(
            discovery.mcpservers_provider, "fetch_top", return_value=community_entries
        ),
    ):
        caps = discovery.discover_popular()

    assert len(caps) == 1


# ---------------------------------------------------------------------------
# discover_by_category
# ---------------------------------------------------------------------------


def test_discover_by_category_filters_by_keyword() -> None:
    discovery = DynamicToolDiscovery()

    entries = [
        _make_entry("github-mcp", "GitHub", 1),
        _make_entry("slack-mcp", "Slack", 2),
        _make_entry("postgres-mcp", "PostgreSQL", 3),
    ]

    with patch.object(discovery.registry_provider, "fetch_top", return_value=entries):
        caps = discovery.discover_by_category("git")

    names = [c.name for c in caps]
    assert "GitHub" in names
    assert "Slack" not in names


def test_discover_by_category_unknown_category_uses_raw_keyword() -> None:
    discovery = DynamicToolDiscovery()

    entries = [_make_entry("weather-api", "Weather Service", 1)]

    with patch.object(discovery.registry_provider, "fetch_top", return_value=entries):
        caps = discovery.discover_by_category("weather")

    assert any(c.name == "Weather Service" for c in caps)


# ---------------------------------------------------------------------------
# AutoInstallToolProvider.generate_config
# ---------------------------------------------------------------------------


def test_generate_config_produces_mcp_servers_dict() -> None:
    provider = AutoInstallToolProvider(auto_install_top_n=2)
    caps = [
        ToolCapability(
            name="GitHub",
            slug="github-mcp",
            category="git",
            popularity_rank=1,
            installation_command=["npx", "-y", "github-mcp"],
        ),
        ToolCapability(
            name="Slack",
            slug="slack-mcp",
            category="communication",
            popularity_rank=2,
            installation_command=["npx", "-y", "slack-mcp"],
        ),
    ]

    with patch.object(provider.discovery, "discover_popular", return_value=caps):
        config = provider.generate_config()

    assert "mcpServers" in config
    assert "github-mcp" in config["mcpServers"]
    assert "slack-mcp" in config["mcpServers"]


def test_generate_config_deduplicates_by_slug() -> None:
    provider = AutoInstallToolProvider(auto_install_top_n=2, categories=["git"])
    caps = [
        ToolCapability(
            name="GitHub",
            slug="github-mcp",
            category="git",
            popularity_rank=1,
            installation_command=["npx", "-y", "github-mcp"],
        )
    ]

    with (
        patch.object(provider.discovery, "discover_popular", return_value=caps),
        patch.object(provider.discovery, "discover_by_category", return_value=caps),
    ):
        config = provider.generate_config()

    # "github-mcp" should appear exactly once despite being in both lists
    assert list(config["mcpServers"].keys()).count("github-mcp") == 1


def test_generate_config_skips_tools_without_install_command() -> None:
    provider = AutoInstallToolProvider(auto_install_top_n=1)
    caps = [
        ToolCapability(
            name="No Command",
            slug="no-cmd",
            category="general",
            popularity_rank=1,
            installation_command=None,
        )
    ]

    with patch.object(provider.discovery, "discover_popular", return_value=caps):
        config = provider.generate_config()

    assert "no-cmd" not in config.get("mcpServers", {})


# ---------------------------------------------------------------------------
# AutoInstallToolProvider.save_config
# ---------------------------------------------------------------------------


def test_save_config_writes_valid_json(tmp_path: Path) -> None:
    config_path = tmp_path / "auto-mcp.json"
    provider = AutoInstallToolProvider(auto_install_top_n=0, config_path=config_path)

    with patch.object(provider.discovery, "discover_popular", return_value=[]):
        saved_path = provider.save_config()

    assert saved_path == config_path
    assert config_path.exists()
    data = json.loads(config_path.read_text())
    assert "mcpServers" in data
