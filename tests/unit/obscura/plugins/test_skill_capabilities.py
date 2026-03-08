"""Tests for skill capability manifests and ContextLoader wiring."""

from __future__ import annotations

from pathlib import Path

from obscura.plugins.builtins import BUILTINS_DIR
from obscura.plugins.manifest import parse_manifest_file
from obscura.core.context import ContextLoader
from obscura.core.types import Backend


# Collect all skill-*.yaml paths once for parametrized tests
_SKILL_YAMLS = sorted(BUILTINS_DIR.glob("skill-*.yaml"))
_SKILL_IDS = [p.stem for p in _SKILL_YAMLS]


def test_skill_manifests_parse() -> None:
    """All 7 skill-*.yaml files parse successfully via parse_manifest_file."""
    assert len(_SKILL_YAMLS) == 7, f"Expected 7 skill manifests, found {len(_SKILL_YAMLS)}"
    for path in _SKILL_YAMLS:
        spec = parse_manifest_file(path)
        assert spec.id == path.stem
        assert spec.name != ""


def test_skill_capabilities_default_grant_false() -> None:
    """All skill capabilities have default_grant=false."""
    for path in _SKILL_YAMLS:
        spec = parse_manifest_file(path)
        assert len(spec.capabilities) > 0, f"{spec.id} should have capabilities"
        for cap in spec.capabilities:
            assert cap.default_grant is False, (
                f"{spec.id} capability {cap.id} should have default_grant=False"
            )


def test_skill_capabilities_content_runtime() -> None:
    """All skill manifests have runtime_type='content'."""
    for path in _SKILL_YAMLS:
        spec = parse_manifest_file(path)
        assert spec.runtime_type == "content", (
            f"{spec.id} should have runtime_type='content', got {spec.runtime_type!r}"
        )


def test_skill_capabilities_no_tools() -> None:
    """All skill capabilities have empty tools list."""
    for path in _SKILL_YAMLS:
        spec = parse_manifest_file(path)
        for cap in spec.capabilities:
            assert cap.tools == (), (
                f"{spec.id} capability {cap.id} should have empty tools, got {cap.tools!r}"
            )


def test_context_loader_accepts_resolver_params() -> None:
    """ContextLoader constructor accepts capability_resolver and agent_id."""
    # Just verify the constructor does not raise with these keyword args
    loader = ContextLoader(
        backend=Backend.COPILOT,
        capability_resolver=object(),
        agent_id="test-agent",
    )
    assert loader._capability_resolver is not None
    assert loader._agent_id == "test-agent"
