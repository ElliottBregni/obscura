"""Plugin bootstrapper — installs runtime dependencies declared in manifests.

Handles pip, uv, npx, npm, cargo, and binary-check dependencies.
Called by the loader pipeline *before* handler resolution.

Usage::

    from obscura.plugins.bootstrapper import run_bootstrap

    result = run_bootstrap(spec)
    if not result.ok:
        print(f"Bootstrap failed: {result.errors}")
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obscura.plugins.models import BootstrapDep, BootstrapSpec, PluginSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Obscura venv resolution
# ---------------------------------------------------------------------------


def _obscura_venv_python() -> str:
    """Return the obscura venv Python, or sys.executable as fallback."""
    venv_dir = Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura")) / "venv"
    venv_python = venv_dir / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def _obscura_venv_bin() -> Path:
    """Return the bin directory of the obscura venv, or sys.prefix/bin."""
    venv_dir = Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura")) / "venv"
    venv_bin = venv_dir / "bin"
    if venv_bin.is_dir():
        return venv_bin
    return Path(sys.prefix) / "bin"


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class BootstrapResult:
    """Outcome of running bootstrap for a plugin."""

    plugin_id: str
    ok: bool = True
    installed: list[str] = field(default_factory=list)   # deps that were installed
    skipped: list[str] = field(default_factory=list)      # already present
    errors: list[str] = field(default_factory=list)       # hard failures
    warnings: list[str] = field(default_factory=list)     # optional dep failures


# ---------------------------------------------------------------------------
# Dependency checkers
# ---------------------------------------------------------------------------


def _is_pip_installed(package: str) -> bool:
    """Check if a pip package is installed in the obscura venv.

    Prefers ``uv pip show`` (works without pip in the venv), falls back
    to ``python -m pip show`` for environments without uv.
    """
    name = package.split("[")[0].split(">=")[0].split("==")[0].split("<")[0].strip()
    venv_python = _obscura_venv_python()
    # Prefer uv — it doesn't need pip inside the venv
    if shutil.which("uv") is not None:
        try:
            result = subprocess.run(
                ["uv", "pip", "show", name, "--python", venv_python],
                capture_output=True, text=True, timeout=15,
            )
            return result.returncode == 0
        except Exception:
            pass
    # Fallback to pip
    try:
        result = subprocess.run(
            [venv_python, "-m", "pip", "show", name],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def _is_binary_available(name: str) -> bool:
    """Check if a binary is on PATH or in the obscura venv bin dir."""
    # Check the obscura venv bin directory first
    obscura_bin = _obscura_venv_bin() / name
    if obscura_bin.is_file():
        return True
    if shutil.which(name) is not None:
        return True
    # Check ~/.local/bin (user pip installs)
    local_bin = Path.home() / ".local" / "bin" / name
    if local_bin.is_file():
        return True
    return False


def _is_npm_installed(package: str) -> bool:
    """Check if an npm package is installed globally."""
    try:
        result = subprocess.run(
            ["npm", "list", "-g", package, "--depth=0"],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Installers
# ---------------------------------------------------------------------------


def _install_pip(dep: BootstrapDep) -> tuple[bool, str]:
    """Install a pip package into the obscura venv.

    Prefers ``uv pip install`` (works without pip in the venv), falls
    back to ``python -m pip install`` for environments without uv.
    """
    pkg = dep.package + dep.version if dep.version else dep.package
    venv_python = _obscura_venv_python()
    # Prefer uv — it doesn't need pip inside the venv
    if shutil.which("uv") is not None:
        try:
            result = subprocess.run(
                ["uv", "pip", "install", pkg, "--python", venv_python],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                return True, ""
            # Don't fall through — uv gave a real answer
            return False, result.stderr.strip()
        except Exception:
            pass  # fall through to pip
    # Fallback to pip
    try:
        result = subprocess.run(
            [venv_python, "-m", "pip", "install", "--quiet", pkg],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def _install_uv(dep: BootstrapDep) -> tuple[bool, str]:
    """Install via uv pip install into the obscura venv."""
    if not _is_binary_available("uv"):
        return _install_pip(dep)  # fallback to pip

    pkg = dep.package + dep.version if dep.version else dep.package
    venv_python = _obscura_venv_python()
    try:
        result = subprocess.run(
            ["uv", "pip", "install", pkg, "--python", venv_python],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def _install_npx(dep: BootstrapDep) -> tuple[bool, str]:
    """npx packages are run on-demand, just verify npx is available."""
    if _is_binary_available("npx"):
        return True, ""
    return False, "npx not found — install Node.js"


def _install_npm(dep: BootstrapDep) -> tuple[bool, str]:
    """Install an npm package globally."""
    pkg = dep.package
    try:
        result = subprocess.run(
            ["npm", "install", "-g", pkg],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def _install_cargo(dep: BootstrapDep) -> tuple[bool, str]:
    """Install via cargo install."""
    if not _is_binary_available("cargo"):
        return False, "cargo not found — install Rust toolchain"
    pkg = dep.package
    try:
        result = subprocess.run(
            ["cargo", "install", pkg],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def _check_binary(dep: BootstrapDep) -> tuple[bool, str]:
    """Verify a binary is on PATH (no install — just a check)."""
    if _is_binary_available(dep.package):
        return True, ""
    return False, f"Binary '{dep.package}' not found on PATH"


def _install_brew(dep: BootstrapDep) -> tuple[bool, str]:
    """Install via Homebrew (macOS/Linux)."""
    if not _is_binary_available("brew"):
        return False, "brew not found — install Homebrew: https://brew.sh"
    pkg = dep.package
    try:
        result = subprocess.run(
            ["brew", "install", pkg],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def _install_pipx(dep: BootstrapDep) -> tuple[bool, str]:
    """Install via pipx (isolated CLI tool installs)."""
    # Try pipx first, fall back to uv tool install, then pip
    for cmd in (["pipx", "install"], ["uv", "tool", "install"]):
        if not _is_binary_available(cmd[0]):
            continue
        try:
            result = subprocess.run(
                [*cmd, dep.package],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                return True, ""
            if "already" in result.stderr.lower():
                return True, ""
        except Exception:
            continue
    # Final fallback: pip install into current env
    return _install_pip(dep)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _brew_binary_name(package: str) -> str:
    """Extract the binary name from a brew package spec.

    Tap formulas like ``nats-io/nats-tools/nats`` install a binary
    named ``nats`` (the last path component).  Plain names like ``fd``
    are returned as-is.
    """
    return package.rsplit("/", 1)[-1]


_INSTALLERS = {
    "pip": (_is_pip_installed, _install_pip),
    "uv": (_is_pip_installed, _install_uv),
    "npx": (lambda p: _is_binary_available("npx"), _install_npx),
    "npm": (_is_npm_installed, _install_npm),
    "cargo": (_is_binary_available, _install_cargo),
    "binary": (_is_binary_available, _check_binary),
    "brew": (lambda p: _is_binary_available(_brew_binary_name(p)), _install_brew),
    "pipx": (lambda p: _is_binary_available(p), _install_pipx),
}


def _bootstrap_dep(dep: BootstrapDep) -> tuple[str, bool, str]:
    """Bootstrap a single dependency. Returns (action, success, error)."""
    checker, installer = _INSTALLERS.get(dep.type, (None, None))
    if checker is None or installer is None:
        return "error", False, f"Unknown dep type: {dep.type}"

    # Check if already present
    try:
        if checker(dep.package):
            return "skipped", True, ""
    except Exception:
        pass

    # Install
    logger.info("Installing %s dependency: %s %s", dep.type, dep.package, dep.version)
    ok, err = installer(dep)
    if ok:
        return "installed", True, ""
    return "error", False, err


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_bootstrap(spec: PluginSpec) -> BootstrapResult:
    """Run the bootstrap pipeline for a plugin.

    Checks each declared dependency and installs if missing.
    Returns a result with installed/skipped/error lists.
    """
    result = BootstrapResult(plugin_id=spec.id)

    if spec.bootstrap is None:
        return result

    bootstrap = spec.bootstrap

    for dep in bootstrap.deps:
        action, ok, err = _bootstrap_dep(dep)
        label = f"{dep.type}:{dep.package}"

        if action == "skipped":
            result.skipped.append(label)
        elif action == "installed":
            result.installed.append(label)
            logger.info("Installed %s for plugin %s", label, spec.id)
        else:
            if dep.optional:
                result.warnings.append(f"{label}: {err}")
                logger.debug("Optional dep %s failed for %s: %s", label, spec.id, err)
            else:
                result.errors.append(f"{label}: {err}")
                result.ok = False
                logger.debug("Required dep %s failed for %s: %s", label, spec.id, err)

    # Run post_install command if all required deps succeeded
    if result.ok and bootstrap.post_install:
        try:
            proc = subprocess.run(
                bootstrap.post_install, shell=True,
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                result.warnings.append(f"post_install failed: {proc.stderr.strip()}")
        except Exception as exc:
            result.warnings.append(f"post_install failed: {exc}")

    # Run check_command to verify
    if result.ok and bootstrap.check_command:
        try:
            proc = subprocess.run(
                bootstrap.check_command, shell=True,
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                result.warnings.append(f"check_command failed: {proc.stderr.strip()}")
        except Exception as exc:
            result.warnings.append(f"check_command failed: {exc}")

    return result


__all__ = [
    "run_bootstrap",
    "BootstrapResult",
]
