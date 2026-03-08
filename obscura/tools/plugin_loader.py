"""Plugin discovery and registration utilities for Obscura.

This module supports two discovery mechanisms (configurable):

- entry points: author packages expose an entry point under the "obscura.tool_provider" group.
  The entry point may resolve to a provider class, a provider factory (callable) or a provider instance.

- local plugins directory: a `plugins/` directory (default cwd/plugins) containing Python modules or
  packages. Each module must expose one of: `get_provider()` callable, `provider` object, or
  `Provider` class. The loader will instantiate classes and call factories as needed.

Providers must follow the ToolProvider protocol (async install(context), async uninstall(context)).

Usage:
    from obscura.tools.plugin_loader import register_plugins
    register_plugins(provider_registry)

"""
from __future__ import annotations

import importlib
import importlib.metadata as metadata
import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "obscura.tool_provider"
PLUGIN_DIR_ENV = os.environ.get("OBSCURA_PLUGIN_DIR", "").strip()
if PLUGIN_DIR_ENV:
    DEFAULT_PLUGIN_DIR = Path(PLUGIN_DIR_ENV).expanduser().resolve()
else:
    from obscura.core.paths import resolve_obscura_home
    DEFAULT_PLUGIN_DIR = resolve_obscura_home() / "plugins"


def _instantiate_provider(candidate: Any) -> Any | None:
    """Normalize a loaded candidate into a provider instance or None on failure."""
    try:
        # If it's a module, look for standard hooks
        if isinstance(candidate, ModuleType):
            for attr in ("get_provider", "provider", "Provider", "make_provider"):
                if hasattr(candidate, attr):
                    val = getattr(candidate, attr)
                    if callable(val):
                        try:
                            return val()
                        except TypeError:
                            # Could be a class; return as-is to be instantiated below
                            return val
                    else:
                        return val
            return None

        # If it's a class, instantiate
        if isinstance(candidate, type):
            try:
                return candidate()
            except Exception as exc:
                logger.exception("Failed to instantiate provider class %s: %s", candidate, exc)
                return None

        # If it's callable (factory), call it
        if callable(candidate):
            try:
                return candidate()
            except TypeError:
                # Callable requires arguments; treat as unusable
                logger.warning("Provider factory %s requires arguments; skipping", candidate)
                return None
            except Exception as exc:
                logger.exception("Error while calling provider factory %s: %s", candidate, exc)
                return None

        # If it's already an instance-like object, return it
        return candidate
    except Exception as exc:
        logger.exception("_instantiate_provider failed: %s", exc)
        return None


def _load_entry_point_providers(group: str = ENTRY_POINT_GROUP) -> list[Any]:
    providers: list[Any] = []
    try:
        eps = metadata.entry_points()
        if hasattr(eps, "select"):
            selected = eps.select(group=group)
        else:
            selected = eps.get(group, [])

        for ep in selected:
            try:
                obj = ep.load()
            except Exception as exc:
                logger.exception("Failed to load entry point %s: %s", ep, exc)
                continue
            prov = _instantiate_provider(obj)
            if prov is None:
                logger.warning("Entry point %s did not yield a provider", ep)
                continue
            providers.append(prov)
    except Exception as exc:
        logger.debug("Could not read entry points: %s", exc)
    return providers


def _import_module_from_path(path: Path) -> ModuleType | None:
    try:
        name = f"obscura.plugin_{path.stem}"
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    except Exception as exc:
        logger.exception("Failed to import plugin module %s: %s", path, exc)
        return None


def _load_local_plugins(plugin_dir: Path = DEFAULT_PLUGIN_DIR) -> list[Any]:
    providers: list[Any] = []
    try:
        if not plugin_dir.exists():
            return providers
        for entry in sorted(plugin_dir.iterdir()):
            try:
                if entry.is_dir():
                    init_py = entry / "__init__.py"
                    if not init_py.exists():
                        continue
                    module = _import_module_from_path(init_py)
                elif entry.suffix == ".py":
                    module = _import_module_from_path(entry)
                else:
                    continue

                if module is None:
                    continue

                prov = _instantiate_provider(module)
                if prov is None:
                    logger.debug("No provider exported from local plugin %s", entry)
                    continue
                providers.append(prov)
            except Exception as exc:
                logger.exception("Failed to load local plugin %s: %s", entry, exc)
    except Exception as exc:
        logger.exception("_load_local_plugins error: %s", exc)
    return providers


def register_plugins(provider_registry: Any, plugin_dir: Path | None = None, entry_point_group: str = ENTRY_POINT_GROUP) -> None:
    """Discover providers via entry points and a local plugins directory and register them.

    provider_registry: expected to have an `add(provider)` method (ToolProviderRegistry).
    plugin_dir: path to scan for local plugins; defaults to $OBSCURA_PLUGIN_DIR or ./plugins
    entry_point_group: entry point group name to scan
    """
    try:
        providers: list[Any] = []
        providers.extend(_load_entry_point_providers(entry_point_group))
        if plugin_dir is None:
            plugin_dir = DEFAULT_PLUGIN_DIR
        providers.extend(_load_local_plugins(Path(plugin_dir)))

        for p in providers:
            try:
                provider_registry.add(p)
                logger.debug("Registered plugin provider: %s", getattr(type(p), "__name__", str(p)))
            except Exception as exc:  # registration errors should not stop other plugins
                logger.exception("Failed to add plugin provider %s: %s", p, exc)
    except Exception as exc:
        logger.exception("register_plugins failed: %s", exc)


__all__ = ["register_plugins", "DEFAULT_PLUGIN_DIR", "ENTRY_POINT_GROUP"]
