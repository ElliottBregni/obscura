"""Built-in plugin manifests for Obscura core providers.

Each YAML file in this directory is a plugin manifest for a provider that
ships with Obscura. The plugin loader discovers these automatically.
"""

from __future__ import annotations

from pathlib import Path

BUILTINS_DIR = Path(__file__).parent


def list_builtin_manifests() -> list[Path]:
    """Return paths to all built-in plugin.yaml files."""
    return sorted(BUILTINS_DIR.glob("*.yaml"))


__all__ = ["list_builtin_manifests", "BUILTINS_DIR"]
