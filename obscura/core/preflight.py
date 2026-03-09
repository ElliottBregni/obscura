"""obscura.core.preflight -- Environment preflight checks for agents.

Validates that an agent's declared :class:`EnvironmentManifest` requirements
are satisfied before the agent starts: binaries on PATH, env vars set,
Python version matches, pip packages installed, working directory and
filesystem paths exist.

Usage::

    from obscura.core.preflight import PreflightValidator

    validator = PreflightValidator()
    result = validator.validate(compiled_agent)
    if not result.passed:
        for check in result.errors:
            print(f"FAIL: {check.name}: {check.message}")
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.core.compiler.compiled import CompiledAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Check models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightCheck:
    """A single preflight check result."""

    name: str
    passed: bool
    message: str = ""
    severity: str = "error"  # "error" | "warning"


@dataclass(frozen=True)
class PreflightResult:
    """Aggregate preflight result for an agent."""

    agent_name: str
    checks: tuple[PreflightCheck, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        """True if no checks with severity='error' failed."""
        return all(c.passed for c in self.checks if c.severity == "error")

    @property
    def errors(self) -> list[PreflightCheck]:
        """Return all failed checks with severity='error'."""
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def warnings(self) -> list[PreflightCheck]:
        """Return all failed checks with severity='warning'."""
        return [c for c in self.checks if not c.passed and c.severity == "warning"]


# ---------------------------------------------------------------------------
# Venv helpers (mirrors bootstrapper.py pattern)
# ---------------------------------------------------------------------------


def _obscura_venv_python() -> str:
    """Return the obscura venv Python, or 'python3' as fallback."""
    venv_dir = Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura")) / "venv"
    venv_python = venv_dir / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return "python3"


def _obscura_venv_bin() -> Path:
    """Return the bin directory of the obscura venv."""
    venv_dir = Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura")) / "venv"
    return venv_dir / "bin"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class PreflightValidator:
    """Validates an agent's EnvironmentManifest requirements."""

    def validate(self, agent: CompiledAgent) -> PreflightResult:
        """Run all preflight checks for an agent.

        If the agent has no environment manifest, returns a passing result
        with no checks.
        """
        if agent.env is None:
            return PreflightResult(agent_name=agent.name)

        env = agent.env
        checks: list[PreflightCheck] = []

        # Binaries
        for binary in env.binaries:
            checks.append(self._check_binary(binary))

        # Env vars
        for key, expected in env.env_vars:
            checks.append(self._check_env_var(key, expected))

        # Python version
        if env.python_version:
            checks.append(self._check_python_version(env.python_version))

        # Packages
        for package in env.packages:
            checks.append(self._check_package(package))

        # Working directory
        if env.working_dir:
            checks.append(self._check_path(env.working_dir, "working_dir"))

        # Read paths
        for path in env.read_paths:
            checks.append(self._check_path(path, "read_path"))

        # Write paths
        for path in env.write_paths:
            checks.append(self._check_path(path, "write_path"))

        return PreflightResult(
            agent_name=agent.name,
            checks=tuple(checks),
        )

    # -- Individual checks --------------------------------------------------

    @staticmethod
    def _check_binary(name: str) -> PreflightCheck:
        """Check that a binary is available on PATH or in the obscura venv."""
        venv_bin = _obscura_venv_bin() / name
        if venv_bin.is_file() or shutil.which(name) is not None:
            return PreflightCheck(
                name=f"binary:{name}",
                passed=True,
                message=f"Binary '{name}' found",
            )
        return PreflightCheck(
            name=f"binary:{name}",
            passed=False,
            message=f"Binary '{name}' not found on PATH",
        )

    @staticmethod
    def _check_env_var(key: str, expected: str) -> PreflightCheck:
        """Check that an environment variable is set."""
        actual = os.environ.get(key)
        if actual is None:
            return PreflightCheck(
                name=f"env:{key}",
                passed=False,
                message=f"Environment variable '{key}' not set",
            )
        if expected and actual != expected:
            return PreflightCheck(
                name=f"env:{key}",
                passed=False,
                message=f"Environment variable '{key}' expected '{expected}', got '{actual}'",
                severity="warning",
            )
        return PreflightCheck(
            name=f"env:{key}",
            passed=True,
            message=f"Environment variable '{key}' is set",
        )

    @staticmethod
    def _check_python_version(expected: str) -> PreflightCheck:
        """Check that the venv Python version matches the expected prefix."""
        try:
            result = subprocess.run(
                [_obscura_venv_python(), "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version = result.stdout.strip().replace("Python ", "")
            if version.startswith(expected):
                return PreflightCheck(
                    name="python_version",
                    passed=True,
                    message=f"Python {version} matches {expected}",
                )
            return PreflightCheck(
                name="python_version",
                passed=False,
                message=f"Python {version} does not match expected {expected}",
            )
        except Exception as exc:
            return PreflightCheck(
                name="python_version",
                passed=False,
                message=f"Failed to check Python version: {exc}",
            )

    @staticmethod
    def _check_package(package: str) -> PreflightCheck:
        """Check that a pip package is installed in the obscura venv."""
        name = package.split("[")[0].split(">=")[0].split("==")[0].split("<")[0].strip()
        try:
            result = subprocess.run(
                [_obscura_venv_python(), "-m", "pip", "show", name],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return PreflightCheck(
                    name=f"package:{name}",
                    passed=True,
                    message=f"Package '{name}' is installed",
                )
            return PreflightCheck(
                name=f"package:{name}",
                passed=False,
                message=f"Package '{name}' is not installed",
            )
        except Exception as exc:
            return PreflightCheck(
                name=f"package:{name}",
                passed=False,
                message=f"Failed to check package '{name}': {exc}",
            )

    @staticmethod
    def _check_path(path_str: str, label: str) -> PreflightCheck:
        """Check that a filesystem path exists."""
        p = Path(path_str)
        if p.exists():
            return PreflightCheck(
                name=f"{label}:{path_str}",
                passed=True,
                message=f"Path '{path_str}' exists",
            )
        return PreflightCheck(
            name=f"{label}:{path_str}",
            passed=False,
            message=f"Path '{path_str}' does not exist",
        )


__all__ = [
    "PreflightCheck",
    "PreflightResult",
    "PreflightValidator",
]
