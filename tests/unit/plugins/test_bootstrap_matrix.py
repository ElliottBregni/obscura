"""Cross-runtime x dep-type bootstrap matrix tests.

Verifies that every combination of (dep_type, runtime_type) bootstraps
correctly through run_bootstrap(), and that global vs local source_type
handling is correct in the loader pipeline.

All subprocess/binary checks are mocked -- no real installs.
"""

from __future__ import annotations

import itertools
from unittest.mock import MagicMock, patch

import pytest

from obscura.plugins import bootstrapper as bootstrapper_mod
from obscura.plugins.bootstrapper import BootstrapResult, run_bootstrap
from obscura.plugins.models import BootstrapDep, BootstrapSpec, PluginSpec

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_DEP_TYPES = ["pip", "uv", "npx", "npm", "cargo", "binary", "brew", "pipx"]
ALL_RUNTIME_TYPES = [
    "native", "cli", "sdk", "mcp", "service",
    "content", "npx", "wasm", "docker", "grpc",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_matrix_spec(
    dep_type: str,
    runtime_type: str,
    package: str = "test-pkg",
    optional: bool = False,
) -> PluginSpec:
    """Build a PluginSpec with a single dep of given type and runtime_type."""
    dep = BootstrapDep(type=dep_type, package=package, optional=optional)
    return PluginSpec(
        id=f"matrix-{dep_type}-{runtime_type}",
        name="Matrix Test Plugin",
        version="1.0.0",
        source_type="local",
        runtime_type=runtime_type,
        bootstrap=BootstrapSpec(deps=(dep,)),
    )


# ===================================================================
# 1. Matrix: dep already installed -> skipped
# ===================================================================


@pytest.mark.parametrize(
    "dep_type,runtime_type",
    list(itertools.product(ALL_DEP_TYPES, ALL_RUNTIME_TYPES)),
    ids=lambda combo: f"{combo}" if isinstance(combo, str) else None,
)
def test_dep_already_installed_skipped(dep_type: str, runtime_type: str):
    """When checker returns True, dep should be skipped regardless of runtime_type."""
    spec = _make_matrix_spec(dep_type, runtime_type)

    # _INSTALLERS stores direct function references, so we must replace
    # the checker in the dict itself (module-level patching won't help).
    original = bootstrapper_mod._INSTALLERS[dep_type]
    fake_checker = MagicMock(return_value=True)
    bootstrapper_mod._INSTALLERS[dep_type] = (fake_checker, original[1])
    try:
        result = run_bootstrap(spec)
    finally:
        bootstrapper_mod._INSTALLERS[dep_type] = original

    assert result.ok is True
    assert len(result.skipped) == 1
    assert f"{dep_type}:test-pkg" in result.skipped
    assert result.installed == []
    assert result.errors == []


# ===================================================================
# 2. Matrix: dep installed successfully
# ===================================================================


@pytest.mark.parametrize(
    "dep_type,runtime_type",
    list(itertools.product(ALL_DEP_TYPES, ALL_RUNTIME_TYPES)),
    ids=lambda combo: f"{combo}" if isinstance(combo, str) else None,
)
def test_dep_installed_successfully(dep_type: str, runtime_type: str):
    """When checker returns False and installer succeeds, dep is installed."""
    spec = _make_matrix_spec(dep_type, runtime_type)

    original = bootstrapper_mod._INSTALLERS[dep_type]
    fake_checker = MagicMock(return_value=False)
    fake_installer = MagicMock(return_value=(True, ""))
    bootstrapper_mod._INSTALLERS[dep_type] = (fake_checker, fake_installer)
    try:
        result = run_bootstrap(spec)
    finally:
        bootstrapper_mod._INSTALLERS[dep_type] = original

    assert result.ok is True
    assert f"{dep_type}:test-pkg" in result.installed
    assert result.errors == []


# ===================================================================
# 3. Required dep fails -> error (per dep type)
# ===================================================================


@pytest.mark.parametrize("dep_type", ALL_DEP_TYPES)
def test_required_dep_fails(dep_type: str):
    """Required dep failure -> ok=False, error recorded."""
    spec = _make_matrix_spec(dep_type, "native", optional=False)

    original = bootstrapper_mod._INSTALLERS[dep_type]
    fake_checker = MagicMock(return_value=False)
    fake_installer = MagicMock(return_value=(False, "install failed"))
    bootstrapper_mod._INSTALLERS[dep_type] = (fake_checker, fake_installer)
    try:
        result = run_bootstrap(spec)
    finally:
        bootstrapper_mod._INSTALLERS[dep_type] = original

    assert result.ok is False
    assert len(result.errors) == 1
    assert "test-pkg" in result.errors[0]


# ===================================================================
# 4. Optional dep fails -> warning (per dep type)
# ===================================================================


@pytest.mark.parametrize("dep_type", ALL_DEP_TYPES)
def test_optional_dep_fails(dep_type: str):
    """Optional dep failure -> ok=True, warning recorded."""
    spec = _make_matrix_spec(dep_type, "native", optional=True)

    original = bootstrapper_mod._INSTALLERS[dep_type]
    fake_checker = MagicMock(return_value=False)
    fake_installer = MagicMock(return_value=(False, "install failed"))
    bootstrapper_mod._INSTALLERS[dep_type] = (fake_checker, fake_installer)
    try:
        result = run_bootstrap(spec)
    finally:
        bootstrapper_mod._INSTALLERS[dep_type] = original

    assert result.ok is True
    assert len(result.warnings) == 1
    assert "test-pkg" in result.warnings[0]
    assert result.errors == []


# ===================================================================
# 5. Global vs Local bootstrap (loader pipeline)
# ===================================================================


class TestGlobalLocalBootstrap:
    """Verify _load_spec() handles bootstrap identically for all source_types."""

    def _make_failing_spec(self, source_type: str, trust_level: str = "community") -> PluginSpec:
        dep = BootstrapDep(type="pip", package="bad-pkg")
        return PluginSpec(
            id=f"test-{source_type}",
            name="Test",
            version="1.0.0",
            source_type=source_type,
            trust_level=trust_level,
            runtime_type="native",
            bootstrap=BootstrapSpec(deps=(dep,)),
        )

    def _make_loader(self, *, lenient_builtins: bool = True):
        from obscura.plugins.loader import PluginLoader
        loader = PluginLoader.__new__(PluginLoader)
        loader._registry = MagicMock()
        loader._plugin_dir = MagicMock()
        loader._loaded = {}
        loader._specs = []
        loader._lenient_builtins = lenient_builtins
        return loader

    @patch("obscura.plugins.loader.validate_plugin_spec", return_value=[])
    @patch("obscura.plugins.loader._check_config", return_value=(True, []))
    @patch("obscura.plugins.bootstrapper.run_bootstrap")
    def test_builtin_bootstrap_lenient(self, mock_bootstrap, _mock_config, _mock_validate):
        """Builtin + lenient -> enabled even when bootstrap fails."""
        mock_bootstrap.return_value = BootstrapResult(plugin_id="test-builtin", ok=False, errors=["pip:bad-pkg: fail"])
        loader = self._make_loader(lenient_builtins=True)
        spec = self._make_failing_spec("builtin")
        registry = MagicMock()

        status = loader._load_spec(spec, registry)
        assert status.state == "enabled"

    @patch("obscura.plugins.loader.validate_plugin_spec", return_value=[])
    @patch("obscura.plugins.loader._check_config", return_value=(True, []))
    @patch("obscura.plugins.bootstrapper.run_bootstrap")
    def test_local_bootstrap_strict(self, mock_bootstrap, _mock_config, _mock_validate):
        """Local source -> failed when bootstrap fails."""
        mock_bootstrap.return_value = BootstrapResult(plugin_id="test-local", ok=False, errors=["pip:bad-pkg: fail"])
        loader = self._make_loader(lenient_builtins=True)
        spec = self._make_failing_spec("local")
        registry = MagicMock()

        status = loader._load_spec(spec, registry)
        assert status.state == "failed"

    @patch("obscura.plugins.loader.validate_plugin_spec", return_value=[])
    @patch("obscura.plugins.loader._check_config", return_value=(True, []))
    @patch("obscura.plugins.bootstrapper.run_bootstrap")
    def test_global_user_bootstrap_strict(self, mock_bootstrap, _mock_config, _mock_validate):
        """Global user plugin (source_type=git) -> failed when bootstrap fails."""
        mock_bootstrap.return_value = BootstrapResult(plugin_id="test-git", ok=False, errors=["pip:bad-pkg: fail"])
        loader = self._make_loader(lenient_builtins=True)
        spec = self._make_failing_spec("git")
        registry = MagicMock()

        status = loader._load_spec(spec, registry)
        assert status.state == "failed"

    @patch("obscura.plugins.loader.validate_plugin_spec", return_value=[])
    @patch("obscura.plugins.loader._check_config", return_value=(True, []))
    @patch("obscura.plugins.bootstrapper.run_bootstrap")
    def test_builtin_non_lenient_strict(self, mock_bootstrap, _mock_config, _mock_validate):
        """Builtin + lenient_builtins=False -> failed when bootstrap fails."""
        mock_bootstrap.return_value = BootstrapResult(plugin_id="test-builtin", ok=False, errors=["pip:bad-pkg: fail"])
        loader = self._make_loader(lenient_builtins=False)
        spec = self._make_failing_spec("builtin")
        registry = MagicMock()

        status = loader._load_spec(spec, registry)
        assert status.state == "failed"
