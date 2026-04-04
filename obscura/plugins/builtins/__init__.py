"""Built-in plugin manifests for Obscura core providers.

Each TOML file in this directory is a plugin manifest for a provider that
ships with Obscura. The plugin loader discovers these automatically.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

BUILTINS_DIR = Path(__file__).parent


def list_builtin_manifests() -> list[Path]:
    """Return paths to all built-in plugin manifest files."""
    return sorted(BUILTINS_DIR.glob("*.toml"))


def list_builtin_plugin_ids() -> list[str]:
    """Return all builtin plugin IDs by scanning manifest ``id`` fields."""
    ids: list[str] = []
    for path in list_builtin_manifests():
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            if "id" in data:
                ids.append(str(data["id"]))
        except Exception:  # noqa: BLE001
            continue
    return sorted(ids)


__all__ = ["BUILTINS_DIR", "list_builtin_manifests", "list_builtin_plugin_ids"]
