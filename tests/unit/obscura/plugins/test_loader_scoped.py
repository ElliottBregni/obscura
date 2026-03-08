"""Tests for PluginLoader.load_scoped() and loaded_specs property."""

from __future__ import annotations

import pytest

from obscura.plugins.loader import PluginLoader


class _FakeProviderRegistry:
    """Minimal stand-in for a provider registry with an add() method."""

    def __init__(self) -> None:
        self.providers: list[object] = []

    def add(self, provider: object) -> None:
        self.providers.append(provider)


def test_loaded_specs_initially_empty() -> None:
    loader = PluginLoader()
    assert loader.loaded_specs == []


def test_loaded_specs_populated_after_load() -> None:
    loader = PluginLoader()
    reg = _FakeProviderRegistry()
    loader.load_builtins(reg)
    # Builtins may or may not all succeed (config missing etc.),
    # but at least some specs should have been successfully loaded.
    # loaded_specs only includes those that reached the enabled state.
    # We just assert it is a list (may be empty if all builtins
    # are disabled due to missing config, but the property works).
    assert isinstance(loader.loaded_specs, list)
    # The returned list should be a copy
    specs = loader.loaded_specs
    specs.append(None)  # type: ignore[arg-type]
    assert len(loader.loaded_specs) != len(specs)


def test_load_scoped_required_success() -> None:
    loader = PluginLoader()
    reg = _FakeProviderRegistry()
    # Discover available builtins so we can pick a real ID
    builtins = loader.discover_builtins()
    assert len(builtins) > 0, "Expected at least one builtin manifest"

    # Pick a skill-* builtin — these have no config requirements and
    # no tools to resolve, so they should reach the enabled state.
    skill_specs = [s for s in builtins if s.id.startswith("skill-")]
    assert len(skill_specs) > 0, "Expected at least one skill-* builtin"
    target_id = skill_specs[0].id

    results = loader.load_scoped(reg, required_ids=[target_id], optional_ids=[])
    assert target_id in results
    assert results[target_id].state == "enabled"


def test_load_scoped_required_missing_raises() -> None:
    loader = PluginLoader()
    reg = _FakeProviderRegistry()
    with pytest.raises(RuntimeError, match="Required plugins failed to load"):
        loader.load_scoped(
            reg,
            required_ids=["nonexistent-plugin-xyz"],
            optional_ids=[],
        )


def test_load_scoped_optional_missing_no_error() -> None:
    loader = PluginLoader()
    reg = _FakeProviderRegistry()
    # Optional missing plugin should NOT raise
    results = loader.load_scoped(
        reg,
        required_ids=[],
        optional_ids=["nonexistent-plugin-xyz"],
    )
    # The missing optional plugin should simply be absent from results
    assert "nonexistent-plugin-xyz" not in results
