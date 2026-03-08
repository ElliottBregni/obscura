"""Tests for capability resolver wiring into AgentConfig."""

from __future__ import annotations

from unittest.mock import patch

from obscura.agent.agents import AgentConfig
from obscura.manifest.models import (
    AgentManifest,
    CapabilityConfig,
    PluginDepsConfig,
)


def test_agent_config_has_capabilities_field() -> None:
    cfg = AgentConfig(name="test", provider="copilot")
    assert isinstance(cfg.capabilities, dict)
    assert cfg.capabilities == {}


def test_agent_config_has_plugins_field() -> None:
    cfg = AgentConfig(name="test", provider="copilot")
    assert isinstance(cfg.plugins, dict)
    assert cfg.plugins == {}


def test_agent_config_from_manifest_maps_capabilities() -> None:
    manifest = AgentManifest(
        name="cap-test",
        capabilities=CapabilityConfig(
            grant=["repo.read", "shell.exec"],
            deny=["net.write"],
        ),
    )
    cfg = AgentConfig.from_manifest(manifest)
    assert cfg.capabilities == {
        "grant": ["repo.read", "shell.exec"],
        "deny": ["net.write"],
    }


def test_agent_config_from_manifest_maps_plugins() -> None:
    manifest = AgentManifest(
        name="plug-test",
        plugins=PluginDepsConfig(
            require=["websearch"],
            optional=["notion", "hf"],
        ),
    )
    cfg = AgentConfig.from_manifest(manifest)
    assert cfg.plugins == {
        "require": ["websearch"],
        "optional": ["notion", "hf"],
    }
