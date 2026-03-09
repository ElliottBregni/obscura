"""Tests for obscura.core.preflight — PreflightValidator and models."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from obscura.core.compiler.compiled import CompiledAgent, EnvironmentManifest
from obscura.core.preflight import PreflightCheck, PreflightResult, PreflightValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_agent(**overrides) -> CompiledAgent:
    defaults = dict(
        name="test-agent",
        template_name="base",
        mode="code",
        agent_type="loop",
        provider="claude",
    )
    defaults.update(overrides)
    return CompiledAgent(**defaults)


def _agent_with_env(**env_overrides) -> CompiledAgent:
    env = EnvironmentManifest(**env_overrides)
    return _minimal_agent(env=env)


# ---------------------------------------------------------------------------
# PreflightCheck model
# ---------------------------------------------------------------------------


class TestPreflightCheck:
    def test_passed_check(self) -> None:
        c = PreflightCheck(name="test", passed=True)
        assert c.passed is True

    def test_failed_check(self) -> None:
        c = PreflightCheck(name="test", passed=False, message="missing")
        assert c.passed is False
        assert c.message == "missing"

    def test_default_severity_error(self) -> None:
        c = PreflightCheck(name="test", passed=False)
        assert c.severity == "error"

    def test_warning_severity(self) -> None:
        c = PreflightCheck(name="test", passed=False, severity="warning")
        assert c.severity == "warning"

    def test_frozen(self) -> None:
        c = PreflightCheck(name="test", passed=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            c.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PreflightResult model
# ---------------------------------------------------------------------------


class TestPreflightResult:
    def test_all_passed(self) -> None:
        r = PreflightResult(
            agent_name="ag",
            checks=(
                PreflightCheck(name="a", passed=True),
                PreflightCheck(name="b", passed=True),
            ),
        )
        assert r.passed is True

    def test_one_error_fails(self) -> None:
        r = PreflightResult(
            agent_name="ag",
            checks=(
                PreflightCheck(name="a", passed=True),
                PreflightCheck(name="b", passed=False, message="bad"),
            ),
        )
        assert r.passed is False

    def test_warning_does_not_fail(self) -> None:
        r = PreflightResult(
            agent_name="ag",
            checks=(
                PreflightCheck(name="a", passed=True),
                PreflightCheck(name="b", passed=False, severity="warning"),
            ),
        )
        assert r.passed is True

    def test_errors_property(self) -> None:
        r = PreflightResult(
            agent_name="ag",
            checks=(
                PreflightCheck(name="ok", passed=True),
                PreflightCheck(name="bad", passed=False, message="fail"),
                PreflightCheck(name="warn", passed=False, severity="warning"),
            ),
        )
        assert len(r.errors) == 1
        assert r.errors[0].name == "bad"

    def test_warnings_property(self) -> None:
        r = PreflightResult(
            agent_name="ag",
            checks=(
                PreflightCheck(name="ok", passed=True),
                PreflightCheck(name="warn", passed=False, severity="warning", message="w"),
            ),
        )
        assert len(r.warnings) == 1
        assert r.warnings[0].name == "warn"

    def test_empty_checks_passes(self) -> None:
        r = PreflightResult(agent_name="ag")
        assert r.passed is True
        assert r.errors == []
        assert r.warnings == []

    def test_frozen(self) -> None:
        r = PreflightResult(agent_name="ag")
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.agent_name = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PreflightValidator: binaries
# ---------------------------------------------------------------------------


class TestPreflightValidatorBinaries:
    @patch("obscura.core.preflight.shutil.which", return_value="/usr/bin/ruff")
    @patch("obscura.core.preflight._obscura_venv_bin")
    def test_binary_exists(self, mock_venv_bin: MagicMock, mock_which: MagicMock) -> None:
        mock_venv_bin.return_value = Path("/nonexistent/venv/bin")
        agent = _agent_with_env(binaries=("ruff",))
        result = PreflightValidator().validate(agent)
        binary_checks = [c for c in result.checks if c.name == "binary:ruff"]
        assert len(binary_checks) == 1
        assert binary_checks[0].passed is True

    @patch("obscura.core.preflight.shutil.which", return_value=None)
    @patch("obscura.core.preflight._obscura_venv_bin")
    def test_binary_missing(self, mock_venv_bin: MagicMock, mock_which: MagicMock) -> None:
        mock_venv_bin.return_value = Path("/nonexistent/venv/bin")
        agent = _agent_with_env(binaries=("nonexistent_tool",))
        result = PreflightValidator().validate(agent)
        binary_checks = [c for c in result.checks if "binary:" in c.name]
        assert len(binary_checks) == 1
        assert binary_checks[0].passed is False


# ---------------------------------------------------------------------------
# PreflightValidator: env vars
# ---------------------------------------------------------------------------


class TestPreflightValidatorEnvVars:
    def test_env_var_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_PREFLIGHT_VAR", "hello")
        agent = _agent_with_env(env_vars=(("TEST_PREFLIGHT_VAR", ""),))
        result = PreflightValidator().validate(agent)
        env_checks = [c for c in result.checks if "env:" in c.name]
        assert len(env_checks) == 1
        assert env_checks[0].passed is True

    def test_env_var_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEFINITELY_NOT_SET_12345", raising=False)
        agent = _agent_with_env(env_vars=(("DEFINITELY_NOT_SET_12345", ""),))
        result = PreflightValidator().validate(agent)
        env_checks = [c for c in result.checks if "env:" in c.name]
        assert len(env_checks) == 1
        assert env_checks[0].passed is False

    def test_env_var_value_mismatch_is_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "actual")
        agent = _agent_with_env(env_vars=(("TEST_VAR", "expected"),))
        result = PreflightValidator().validate(agent)
        env_checks = [c for c in result.checks if "env:" in c.name]
        assert env_checks[0].passed is False
        assert env_checks[0].severity == "warning"


# ---------------------------------------------------------------------------
# PreflightValidator: python version
# ---------------------------------------------------------------------------


class TestPreflightValidatorPythonVersion:
    @patch("obscura.core.preflight.subprocess.run")
    def test_python_version_matches(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            stdout="Python 3.13.0\n",
            returncode=0,
        )
        agent = _agent_with_env(python_version="3.13")
        result = PreflightValidator().validate(agent)
        py_checks = [c for c in result.checks if c.name == "python_version"]
        assert len(py_checks) == 1
        assert py_checks[0].passed is True

    @patch("obscura.core.preflight.subprocess.run")
    def test_python_version_mismatch(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            stdout="Python 3.12.7\n",
            returncode=0,
        )
        agent = _agent_with_env(python_version="3.13")
        result = PreflightValidator().validate(agent)
        py_checks = [c for c in result.checks if c.name == "python_version"]
        assert py_checks[0].passed is False

    @patch("obscura.core.preflight.subprocess.run", side_effect=FileNotFoundError("no python"))
    def test_python_version_error(self, mock_run: MagicMock) -> None:
        agent = _agent_with_env(python_version="3.13")
        result = PreflightValidator().validate(agent)
        py_checks = [c for c in result.checks if c.name == "python_version"]
        assert py_checks[0].passed is False
        assert "Failed" in py_checks[0].message


# ---------------------------------------------------------------------------
# PreflightValidator: packages
# ---------------------------------------------------------------------------


class TestPreflightValidatorPackages:
    @patch("obscura.core.preflight.subprocess.run")
    def test_package_installed(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        agent = _agent_with_env(packages=("requests>=2.31.0",))
        result = PreflightValidator().validate(agent)
        pkg_checks = [c for c in result.checks if "package:" in c.name]
        assert len(pkg_checks) == 1
        assert pkg_checks[0].passed is True

    @patch("obscura.core.preflight.subprocess.run")
    def test_package_missing(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        agent = _agent_with_env(packages=("nonexistent_pkg",))
        result = PreflightValidator().validate(agent)
        pkg_checks = [c for c in result.checks if "package:" in c.name]
        assert pkg_checks[0].passed is False


# ---------------------------------------------------------------------------
# PreflightValidator: paths
# ---------------------------------------------------------------------------


class TestPreflightValidatorPaths:
    def test_working_dir_exists(self, tmp_path: Path) -> None:
        agent = _agent_with_env(working_dir=str(tmp_path))
        result = PreflightValidator().validate(agent)
        path_checks = [c for c in result.checks if "working_dir:" in c.name]
        assert len(path_checks) == 1
        assert path_checks[0].passed is True

    def test_working_dir_missing(self) -> None:
        agent = _agent_with_env(working_dir="/definitely/not/a/real/path")
        result = PreflightValidator().validate(agent)
        path_checks = [c for c in result.checks if "working_dir:" in c.name]
        assert path_checks[0].passed is False

    def test_read_path_exists(self, tmp_path: Path) -> None:
        agent = _agent_with_env(read_paths=(str(tmp_path),))
        result = PreflightValidator().validate(agent)
        path_checks = [c for c in result.checks if "read_path:" in c.name]
        assert path_checks[0].passed is True

    def test_read_path_missing(self) -> None:
        agent = _agent_with_env(read_paths=("/no/such/dir",))
        result = PreflightValidator().validate(agent)
        path_checks = [c for c in result.checks if "read_path:" in c.name]
        assert path_checks[0].passed is False

    def test_write_path_exists(self, tmp_path: Path) -> None:
        agent = _agent_with_env(write_paths=(str(tmp_path),))
        result = PreflightValidator().validate(agent)
        path_checks = [c for c in result.checks if "write_path:" in c.name]
        assert path_checks[0].passed is True

    def test_write_path_missing(self) -> None:
        agent = _agent_with_env(write_paths=("/no/such/dir",))
        result = PreflightValidator().validate(agent)
        path_checks = [c for c in result.checks if "write_path:" in c.name]
        assert path_checks[0].passed is False


# ---------------------------------------------------------------------------
# PreflightValidator: integration
# ---------------------------------------------------------------------------


class TestPreflightValidatorIntegration:
    def test_agent_without_manifest_passes(self) -> None:
        agent = _minimal_agent()
        result = PreflightValidator().validate(agent)
        assert result.passed is True
        assert result.checks == ()

    def test_empty_manifest_passes(self) -> None:
        agent = _minimal_agent(env=EnvironmentManifest(python_version=""))
        result = PreflightValidator().validate(agent)
        assert result.passed is True

    @patch("obscura.core.preflight.subprocess.run")
    @patch("obscura.core.preflight.shutil.which", return_value=None)
    @patch("obscura.core.preflight._obscura_venv_bin")
    def test_mixed_pass_fail(
        self,
        mock_venv_bin: MagicMock,
        mock_which: MagicMock,
        mock_subprocess: MagicMock,
    ) -> None:
        mock_venv_bin.return_value = Path("/nonexistent/venv/bin")
        mock_subprocess.return_value = MagicMock(
            stdout="Python 3.13.0\n",
            returncode=0,
        )
        agent = _agent_with_env(
            binaries=("missing_tool",),
            python_version="3.13",
        )
        result = PreflightValidator().validate(agent)
        assert not result.passed
        assert len(result.errors) == 1
        assert result.errors[0].name == "binary:missing_tool"

    @patch("obscura.core.preflight.subprocess.run")
    @patch("obscura.core.preflight.shutil.which", return_value=None)
    @patch("obscura.core.preflight._obscura_venv_bin")
    def test_all_checks_run_even_if_first_fails(
        self,
        mock_venv_bin: MagicMock,
        mock_which: MagicMock,
        mock_subprocess: MagicMock,
    ) -> None:
        mock_venv_bin.return_value = Path("/nonexistent/venv/bin")
        mock_subprocess.return_value = MagicMock(
            stdout="Python 3.13.0\n",
            returncode=0,
        )
        agent = _agent_with_env(
            binaries=("missing1", "missing2"),
            python_version="3.13",
        )
        result = PreflightValidator().validate(agent)
        binary_checks = [c for c in result.checks if "binary:" in c.name]
        assert len(binary_checks) == 2
        assert all(not c.passed for c in binary_checks)
