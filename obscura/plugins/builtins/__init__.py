"""Built-in plugin manifests for Obscura core providers.

Each YAML file in this directory is a plugin manifest for a provider that
ships with Obscura. The plugin loader discovers these automatically.
"""

from __future__ import annotations

from pathlib import Path

import yaml

BUILTINS_DIR = Path(__file__).parent


def list_builtin_manifests() -> list[Path]:
    """Return paths to all built-in plugin.yaml files."""
    return sorted(BUILTINS_DIR.glob("*.yaml"))


def list_builtin_plugin_ids() -> list[str]:
    """Return all builtin plugin IDs by scanning manifest ``id`` fields."""
    ids: list[str] = []
    for path in list_builtin_manifests():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "id" in data:
                ids.append(str(data["id"]))
        except Exception:  # noqa: BLE001
            continue
    return sorted(ids)


__all__ = ["list_builtin_manifests", "list_builtin_plugin_ids", "BUILTINS_DIR"]
