"""Comprehensive tests for obscura.plugins.bootstrapper.

All subprocess.run and shutil.which calls are mocked — no real installs happen.
"""

from __future__ import annotations

import subprocess
from dataclasses import fields
from unittest.mock import MagicMock, patch

import pytest

from obscura.plugins import bootstrapper as bootstrapper_mod
from obscura.plugins.bootstrapper import (
    BootstrapResult,
    _bootstrap_dep,
    _check_binary,
    _install_cargo,
    _install_npm,
    _install_npx,
    _install_pip,
    _install_uv,
    _is_binary_available,
    _is_npm_installed,
    _is_pip_installed,
    run_bootstrap,
)
from obscura.plugins.models import BootstrapDep, BootstrapSpec, PluginSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(
    plugin_id: str = "test-plugin",
    bootstrap: BootstrapSpec | None = None,
) -> PluginSpec:
    """Build a minimal PluginSpec for testing."""
    return PluginSpec(
        id=plugin_id,
        name="Test Plugin",
        version="0.1.0",
        source_type="local",
        runtime_type="cli",
        bootstrap=bootstrap,
    )


def _dep(
    dep_type: str = "pip",
    package: str = "some-pkg",
    version: str = "",
    optional: bool = False,
) -> BootstrapDep:
    return BootstrapDep(type=dep_type, package=package, version=version, optional=optional)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ===================================================================
# 1. _is_pip_installed
# ===================================================================

class TestIsPipInstalled:
    """Tests for _is_pip_installed."""

    def test_importable_package_returns_true(self):
        """If __import__ succeeds, package is considered installed."""
        with patch("builtins.__import__", return_value=MagicMock()):
            assert _is_pip_installed("requests") is True

    def test_import_fails_but_pip_show_succeeds(self):
        """Falls back to `pip show` when import fails."""
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fake_import(name, *args, **kwargs):
            if name == "some_pkg":
                raise ImportError("no module")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with patch("subprocess.run", return_value=_proc(returncode=0)) as mock_run:
                assert _is_pip_installed("some-pkg") is True
                # Should call pip show with normalized name
                mock_run.assert_called_once()
                cmd = mock_run.call_args[0][0]
                assert "show" in cmd
                assert "some-pkg" in cmd

    def test_import_fails_and_pip_show_fails(self):
        """Both import and pip show fail → False."""
        def fake_import(name, *args, **kwargs):
            raise ImportError("nope")

        with patch("builtins.__import__", side_effect=fake_import):
            with patch("subprocess.run", return_value=_proc(returncode=1)):
                assert _is_pip_installed("nonexistent") is False

    def test_strips_version_specifiers(self):
        """Handles packages like 'pkg>=1.0' or 'pkg[extra]'."""
        def fake_import(name, *args, **kwargs):
            if name == "my_pkg":
                return MagicMock()
            raise ImportError

        with patch("builtins.__import__", side_effect=fake_import):
            assert _is_pip_installed("my-pkg>=1.0") is True
            assert _is_pip_installed("my-pkg[extra]") is True
            assert _is_pip_installed("my-pkg==2.0") is True

    def test_subprocess_exception_returns_false(self):
        """If subprocess.run itself raises, return False."""
        def fake_import(name, *args, **kwargs):
            raise ImportError

        with patch("builtins.__import__", side_effect=fake_import):
            with patch("subprocess.run", side_effect=OSError("broken")):
                assert _is_pip_installed("pkg") is False


# ===================================================================
# 2. _is_binary_available
# ===================================================================

class TestIsBinaryAvailable:

    def test_binary_found(self):
        with patch("obscura.plugins.bootstrapper.shutil.which", return_value="/usr/bin/foo"):
            assert _is_binary_available("foo") is True

    def test_binary_not_found(self):
        with patch("obscura.plugins.bootstrapper.shutil.which", return_value=None):
            assert _is_binary_available("foo") is False


# ===================================================================
# 3. _is_npm_installed
# ===================================================================

class TestIsNpmInstalled:

    def test_installed(self):
        with patch("subprocess.run", return_value=_proc(returncode=0)):
            assert _is_npm_installed("prettier") is True

    def test_not_installed(self):
        with patch("subprocess.run", return_value=_proc(returncode=1)):
            assert _is_npm_installed("prettier") is False

    def test_subprocess_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("npm not found")):
            assert _is_npm_installed("prettier") is False


# ===================================================================
# 4. _install_pip
# ===================================================================

class TestInstallPip:

    def test_success(self):
        dep = _dep("pip", "requests")
        with patch("subprocess.run", return_value=_proc(returncode=0)):
            ok, err = _install_pip(dep)
        assert ok is True
        assert err == ""

    def test_failure(self):
        dep = _dep("pip", "bad-pkg")
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="ERROR: No matching")):
            ok, err = _install_pip(dep)
        assert ok is False
        assert "No matching" in err

    def test_with_version(self):
        dep = _dep("pip", "requests", version=">=2.0")
        with patch("subprocess.run", return_value=_proc(returncode=0)) as mock_run:
            _install_pip(dep)
            cmd = mock_run.call_args[0][0]
            assert "requests>=2.0" in cmd

    def test_exception(self):
        dep = _dep("pip", "pkg")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=120)):
            ok, err = _install_pip(dep)
        assert ok is False
        assert err  # some error message


# ===================================================================
# 5. _install_uv
# ===================================================================

class TestInstallUv:

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    @patch("subprocess.run")
    def test_uv_available_success(self, mock_run, mock_binary):
        mock_binary.return_value = True
        mock_run.return_value = _proc(returncode=0)
        dep = _dep("uv", "ruff")
        ok, err = _install_uv(dep)
        assert ok is True
        assert err == ""
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "uv"

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    @patch("subprocess.run")
    def test_uv_already_installed_stderr(self, mock_run, mock_binary):
        """'already installed' in stderr → still True."""
        mock_binary.return_value = True
        mock_run.return_value = _proc(returncode=1, stderr="error: ruff is already installed")
        dep = _dep("uv", "ruff")
        ok, err = _install_uv(dep)
        assert ok is True

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    @patch("subprocess.run")
    def test_uv_failure(self, mock_run, mock_binary):
        mock_binary.return_value = True
        mock_run.return_value = _proc(returncode=1, stderr="unknown package")
        dep = _dep("uv", "bad-tool")
        ok, err = _install_uv(dep)
        assert ok is False
        assert "unknown package" in err

    @patch("obscura.plugins.bootstrapper._install_pip")
    @patch("obscura.plugins.bootstrapper._is_binary_available")
    def test_fallback_to_pip(self, mock_binary, mock_pip):
        """When uv is not available, falls back to pip."""
        mock_binary.return_value = False
        mock_pip.return_value = (True, "")
        dep = _dep("uv", "ruff")
        ok, err = _install_uv(dep)
        assert ok is True
        mock_pip.assert_called_once_with(dep)


# ===================================================================
# 6. _install_npx
# ===================================================================

class TestInstallNpx:

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    def test_npx_available(self, mock_binary):
        mock_binary.return_value = True
        ok, err = _install_npx(_dep("npx", "prettier"))
        assert ok is True
        assert err == ""

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    def test_npx_not_available(self, mock_binary):
        mock_binary.return_value = False
        ok, err = _install_npx(_dep("npx", "prettier"))
        assert ok is False
        assert "npx not found" in err


# ===================================================================
# 7. _install_npm
# ===================================================================

class TestInstallNpm:

    def test_success(self):
        dep = _dep("npm", "prettier")
        with patch("subprocess.run", return_value=_proc(returncode=0)):
            ok, err = _install_npm(dep)
        assert ok is True

    def test_failure(self):
        dep = _dep("npm", "bad-pkg")
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="npm ERR!")):
            ok, err = _install_npm(dep)
        assert ok is False
        assert "npm ERR!" in err

    def test_exception(self):
        dep = _dep("npm", "pkg")
        with patch("subprocess.run", side_effect=FileNotFoundError("npm not found")):
            ok, err = _install_npm(dep)
        assert ok is False


# ===================================================================
# 8. _install_cargo
# ===================================================================

class TestInstallCargo:

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    @patch("subprocess.run")
    def test_success(self, mock_run, mock_binary):
        mock_binary.return_value = True
        mock_run.return_value = _proc(returncode=0)
        ok, err = _install_cargo(_dep("cargo", "ripgrep"))
        assert ok is True

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    def test_cargo_not_available(self, mock_binary):
        mock_binary.return_value = False
        ok, err = _install_cargo(_dep("cargo", "ripgrep"))
        assert ok is False
        assert "cargo not found" in err

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    @patch("subprocess.run")
    def test_install_failure(self, mock_run, mock_binary):
        mock_binary.return_value = True
        mock_run.return_value = _proc(returncode=1, stderr="error[E0463]")
        ok, err = _install_cargo(_dep("cargo", "bad-crate"))
        assert ok is False
        assert "error" in err


# ===================================================================
# 9. _check_binary
# ===================================================================

class TestCheckBinary:

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    def test_binary_present(self, mock_binary):
        mock_binary.return_value = True
        ok, err = _check_binary(_dep("binary", "git"))
        assert ok is True

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    def test_binary_missing(self, mock_binary):
        mock_binary.return_value = False
        ok, err = _check_binary(_dep("binary", "missing-tool"))
        assert ok is False
        assert "missing-tool" in err


# ===================================================================
# 10. run_bootstrap
# ===================================================================

class TestRunBootstrap:
    """Integration-level tests for run_bootstrap."""

    def test_no_bootstrap_spec(self):
        """No bootstrap → ok=True, empty lists."""
        spec = _make_spec(bootstrap=None)
        result = run_bootstrap(spec)
        assert result.ok is True
        assert result.installed == []
        assert result.skipped == []
        assert result.errors == []
        assert result.warnings == []
        assert result.plugin_id == "test-plugin"

    @patch("obscura.plugins.bootstrapper._is_pip_installed", return_value=True)
    def test_all_deps_already_installed(self, _mock_check):
        """All deps present → ok=True, all in skipped."""
        bs = BootstrapSpec(deps=(
            _dep("pip", "requests"),
            _dep("pip", "click"),
        ))
        spec = _make_spec(bootstrap=bs)
        result = run_bootstrap(spec)
        assert result.ok is True
        assert len(result.skipped) == 2
        assert result.installed == []
        assert result.errors == []

    @patch("obscura.plugins.bootstrapper._is_pip_installed", return_value=False)
    @patch("subprocess.run", return_value=_proc(returncode=1, stderr="install failed"))
    def test_required_dep_fails(self, _mock_run, _mock_check):
        """Required dep fails → ok=False, error recorded."""
        bs = BootstrapSpec(deps=(_dep("pip", "bad-pkg"),))
        spec = _make_spec(bootstrap=bs)
        result = run_bootstrap(spec)
        assert result.ok is False
        assert len(result.errors) == 1
        assert "bad-pkg" in result.errors[0]

    @patch("obscura.plugins.bootstrapper._is_pip_installed", return_value=False)
    @patch("subprocess.run", return_value=_proc(returncode=1, stderr="install failed"))
    def test_optional_dep_fails(self, _mock_run, _mock_check):
        """Optional dep fails → ok=True, warning recorded."""
        bs = BootstrapSpec(deps=(_dep("pip", "opt-pkg", optional=True),))
        spec = _make_spec(bootstrap=bs)
        result = run_bootstrap(spec)
        assert result.ok is True
        assert len(result.warnings) == 1
        assert "opt-pkg" in result.warnings[0]
        assert result.errors == []

    def test_mix_of_installed_missing_optional(self):
        """Mix: one installed, one required fails, one optional fails."""
        bs = BootstrapSpec(deps=(
            _dep("pip", "present-pkg"),
            _dep("pip", "missing-required"),
            _dep("pip", "missing-optional", optional=True),
        ))
        spec = _make_spec(bootstrap=bs)

        def fake_check(pkg):
            return pkg == "present-pkg"

        # Patch the checker stored in _INSTALLERS since it holds a direct reference
        original = bootstrapper_mod._INSTALLERS["pip"]
        bootstrapper_mod._INSTALLERS["pip"] = (fake_check, original[1])
        try:
            with patch("subprocess.run", return_value=_proc(returncode=1, stderr="fail")):
                result = run_bootstrap(spec)
        finally:
            bootstrapper_mod._INSTALLERS["pip"] = original

        assert result.ok is False
        assert "pip:present-pkg" in result.skipped
        assert any("missing-required" in e for e in result.errors)
        assert any("missing-optional" in w for w in result.warnings)

    @patch("obscura.plugins.bootstrapper._is_pip_installed", return_value=False)
    @patch("subprocess.run")
    def test_post_install_runs_on_success(self, mock_run, _mock_check):
        """post_install command runs when all required deps succeed."""
        # First call: pip install succeeds. Second call: post_install.
        mock_run.return_value = _proc(returncode=0)
        bs = BootstrapSpec(
            deps=(_dep("pip", "pkg"),),
            post_install="echo done",
        )
        spec = _make_spec(bootstrap=bs)
        result = run_bootstrap(spec)
        assert result.ok is True
        # post_install should have been called (shell=True)
        calls = mock_run.call_args_list
        assert any(c.kwargs.get("shell") is True or (len(c.args) > 0 and c.args[0] == "echo done") for c in calls)

    @patch("obscura.plugins.bootstrapper._is_pip_installed", return_value=False)
    @patch("subprocess.run")
    def test_post_install_failure_adds_warning(self, mock_run, _mock_check):
        """post_install failure adds a warning but ok stays True."""
        def side_effect(*args, **kwargs):
            if kwargs.get("shell"):
                return _proc(returncode=1, stderr="post fail")
            return _proc(returncode=0)

        mock_run.side_effect = side_effect
        bs = BootstrapSpec(
            deps=(_dep("pip", "pkg"),),
            post_install="failing-cmd",
        )
        spec = _make_spec(bootstrap=bs)
        result = run_bootstrap(spec)
        assert result.ok is True
        assert any("post_install failed" in w for w in result.warnings)

    @patch("obscura.plugins.bootstrapper._is_pip_installed", return_value=False)
    @patch("subprocess.run")
    def test_check_command_runs_on_success(self, mock_run, _mock_check):
        mock_run.return_value = _proc(returncode=0)
        bs = BootstrapSpec(
            deps=(_dep("pip", "pkg"),),
            check_command="pkg --version",
        )
        spec = _make_spec(bootstrap=bs)
        result = run_bootstrap(spec)
        assert result.ok is True

    @patch("obscura.plugins.bootstrapper._is_pip_installed", return_value=False)
    @patch("subprocess.run")
    def test_check_command_failure_adds_warning(self, mock_run, _mock_check):
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("shell") and "check" in str(args):
                return _proc(returncode=1, stderr="check fail")
            # For pip install and shell check_command (can't distinguish by args alone)
            if kwargs.get("shell"):
                return _proc(returncode=1, stderr="check fail")
            return _proc(returncode=0)

        mock_run.side_effect = side_effect
        bs = BootstrapSpec(
            deps=(_dep("pip", "pkg"),),
            check_command="pkg --version",
        )
        spec = _make_spec(bootstrap=bs)
        result = run_bootstrap(spec)
        assert result.ok is True
        assert any("check_command failed" in w for w in result.warnings)

    def test_post_install_skipped_on_failure(self):
        """post_install does NOT run when a required dep fails."""
        bs = BootstrapSpec(
            deps=(_dep("pip", "bad"),),
            post_install="echo should-not-run",
        )
        spec = _make_spec(bootstrap=bs)
        with patch("obscura.plugins.bootstrapper._is_pip_installed", return_value=False):
            with patch("subprocess.run", return_value=_proc(returncode=1, stderr="fail")) as mock_run:
                result = run_bootstrap(spec)
        assert result.ok is False
        # post_install should not have been called with shell=True
        shell_calls = [c for c in mock_run.call_args_list if c.kwargs.get("shell")]
        assert len(shell_calls) == 0

    @patch("obscura.plugins.bootstrapper._is_binary_available")
    def test_binary_dep_type(self, mock_binary):
        """Binary dep type uses _is_binary_available checker."""
        mock_binary.return_value = True
        bs = BootstrapSpec(deps=(_dep("binary", "git"),))
        spec = _make_spec(bootstrap=bs)
        result = run_bootstrap(spec)
        assert result.ok is True
        assert "binary:git" in result.skipped

    def test_unknown_dep_type_errors(self):
        """An unknown dep type results in an error."""
        # BootstrapDep validates types, so we need to bypass validation
        dep = object.__new__(BootstrapDep)
        object.__setattr__(dep, "type", "unknown")
        object.__setattr__(dep, "package", "pkg")
        object.__setattr__(dep, "version", "")
        object.__setattr__(dep, "optional", False)

        action, ok, err = _bootstrap_dep(dep)
        assert ok is False
        assert "Unknown dep type" in err


# ===================================================================
# 11. BootstrapResult
# ===================================================================

class TestBootstrapResult:
    """Verify BootstrapResult dataclass structure."""

    def test_default_values(self):
        r = BootstrapResult(plugin_id="test")
        assert r.plugin_id == "test"
        assert r.ok is True
        assert r.installed == []
        assert r.skipped == []
        assert r.errors == []
        assert r.warnings == []

    def test_all_fields_present(self):
        names = {f.name for f in fields(BootstrapResult)}
        assert names == {"plugin_id", "ok", "installed", "skipped", "errors", "warnings"}

    def test_mutable_lists(self):
        """Lists are independent between instances."""
        r1 = BootstrapResult(plugin_id="a")
        r2 = BootstrapResult(plugin_id="b")
        r1.installed.append("pip:x")
        assert r2.installed == []
