"""Tests for CapabilityConfig, PluginDepsConfig, and frontmatter parsing."""

from __future__ import annotations

from obscura.manifest.models import (
    AgentManifest,
    CapabilityConfig,
    PluginDepsConfig,
    agent_manifest_from_frontmatter,
)


def test_capability_config_defaults() -> None:
    cfg = CapabilityConfig()
    assert cfg.grant == []
    assert cfg.deny == []


def test_capability_config_with_values() -> None:
    cfg = CapabilityConfig(grant=["a.b", "c.d"], deny=["e.f"])
    assert cfg.grant == ["a.b", "c.d"]
    assert cfg.deny == ["e.f"]


def test_plugin_deps_config_defaults() -> None:
    cfg = PluginDepsConfig()
    assert cfg.require == []
    assert cfg.optional == []


def test_plugin_deps_config_with_values() -> None:
    cfg = PluginDepsConfig(require=["websearch"], optional=["notion"])
    assert cfg.require == ["websearch"]
    assert cfg.optional == ["notion"]


def test_agent_manifest_has_capabilities() -> None:
    m = AgentManifest(name="test")
    assert isinstance(m.capabilities, CapabilityConfig)
    assert m.capabilities.grant == []
    assert m.capabilities.deny == []


def test_agent_manifest_has_plugins() -> None:
    m = AgentManifest(name="test")
    assert isinstance(m.plugins, PluginDepsConfig)
    assert m.plugins.require == []
    assert m.plugins.optional == []


def test_frontmatter_parses_capabilities() -> None:
    metadata = {
        "name": "cap-agent",
        "capabilities": {"grant": ["repo.read", "shell.exec"], "deny": ["net.write"]},
    }
    m = agent_manifest_from_frontmatter(metadata, "body")
    assert m.capabilities.grant == ["repo.read", "shell.exec"]
    assert m.capabilities.deny == ["net.write"]


def test_frontmatter_parses_plugins() -> None:
    metadata = {
        "name": "plug-agent",
        "plugins": {"require": ["websearch"], "optional": ["notion", "hf"]},
    }
    m = agent_manifest_from_frontmatter(metadata, "body")
    assert m.plugins.require == ["websearch"]
    assert m.plugins.optional == ["notion", "hf"]


def test_frontmatter_skills_filter_to_capability_grants() -> None:
    metadata = {
        "name": "filter-agent",
        "skills": {"filter": ["pytight", "red-team"]},
    }
    m = agent_manifest_from_frontmatter(metadata, "body")
    assert "skill.pytight" in m.capabilities.grant
    assert "skill.red_team" in m.capabilities.grant


def test_frontmatter_skills_filter_merges_with_explicit_capabilities() -> None:
    metadata = {
        "name": "merge-agent",
        "capabilities": {"grant": ["repo.read"], "deny": ["net.write"]},
        "skills": {"filter": ["authority"]},
    }
    m = agent_manifest_from_frontmatter(metadata, "body")
    # Both the explicit grant and the skill-derived grant should be present
    assert "repo.read" in m.capabilities.grant
    assert "skill.authority" in m.capabilities.grant
    # Deny list preserved
    assert m.capabilities.deny == ["net.write"]
