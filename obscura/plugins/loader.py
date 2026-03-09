"""Plugin loader pipeline for Obscura.

Structured pipeline that replaces the earlier ``plugin_loader.py``:

    discover → validate_manifest → resolve_config → bootstrap →
    normalize_resources → register → track_health

Lifecycle states: discovered → installed → enabled → active → unhealthy → disabled → failed

The loader works with the ``PluginRegistryService`` for persistence and
the ``ToolProviderRegistry`` (or future separated registries) for runtime
registration.

Usage::

    from obscura.plugins.loader import PluginLoader

    loader = PluginLoader()
    loader.load_all_enabled(provider_registry)
    loader.load_builtins(provider_registry)
"""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
from typing import Any

from obscura.core.paths import resolve_obscura_global_home
from obscura.plugins.manifest import ManifestError, parse_manifest_file
from obscura.plugins.models import (
    PluginSpec,
    PluginStatus,
)
from obscura.plugins.registry import PluginRegistryService
from obscura.plugins.validator import validate_plugin_spec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def _check_config(spec: PluginSpec) -> tuple[bool, list[str]]:
    """Check whether all required config values are available.

    Returns (satisfied, missing_keys).
    """
    missing: list[str] = []
    for req in spec.config_requirements:
        if not req.required:
            continue
        val = os.environ.get(req.key, "").strip()
        if not val and req.default is None:
            missing.append(req.key)
    return (len(missing) == 0, missing)


# ---------------------------------------------------------------------------
# Handler resolution
# ---------------------------------------------------------------------------


def _resolve_handler(ref: str) -> Any | None:
    """Resolve a dotted handler reference like ``pkg.mod:func``.

    Returns the callable or None on failure.
    """
    if not ref:
        return None
    try:
        if ":" in ref:
            module_path, attr_name = ref.rsplit(":", 1)
        else:
            parts = ref.rsplit(".", 1)
            if len(parts) == 2:
                module_path, attr_name = parts
            else:
                return None
        mod = importlib.import_module(module_path)
        attr = getattr(mod, attr_name, None)
        # Unwrap @tool()-decorated functions to get the original handler
        if attr is not None and hasattr(attr, "spec"):
            inner = getattr(attr.spec, "handler", None)
            if inner is not None:
                return inner
        return attr
    except Exception as exc:
        logger.debug("Failed to resolve handler %s: %s", ref, exc)
        return None


# ---------------------------------------------------------------------------
# Provider instantiation from PluginSpec
# ---------------------------------------------------------------------------


class ManifestToolProvider:
    """A ToolProvider created from a PluginSpec manifest.

    Wraps the manifest's tools into ToolSpec instances and registers them
    during install().
    """

    def __init__(self, spec: PluginSpec) -> None:
        self.spec = spec
        self._installed = False

    async def install(self, context: Any) -> None:
        """Register all tools from the manifest."""
        import inspect
        from obscura.core.types import ToolSpec

        raw_allowed: Any = getattr(context, "allowed_tool_names", None)
        allowed: set[str] | None = raw_allowed if isinstance(raw_allowed, set) else None

        for tool_contrib in self.spec.tools:
            if allowed is not None and tool_contrib.name not in allowed:
                continue

            handler = _resolve_handler(tool_contrib.handler_ref)
            if handler is None:
                logger.warning(
                    "Plugin %s: could not resolve handler for tool %s (%s)",
                    self.spec.id, tool_contrib.name, tool_contrib.handler_ref,
                )
                continue

            tool_spec = ToolSpec(
                name=tool_contrib.name,
                description=tool_contrib.description,
                parameters=tool_contrib.parameters,
                handler=handler,
                side_effects=tool_contrib.side_effects,
                required_tier=tool_contrib.required_tier,
                timeout_seconds=tool_contrib.timeout_seconds,
                retries=tool_contrib.retries,
            )

            try:
                result = context.agent.client.register_tool(tool_spec)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning(
                    "Plugin %s: failed to register tool %s: %s",
                    self.spec.id, tool_contrib.name, exc,
                )

        self._installed = True
        logger.info(
            "Plugin %s loaded: %d tools registered",
            self.spec.id, len(self.spec.tools),
        )

    async def uninstall(self, context: Any) -> None:
        self._installed = False


# ---------------------------------------------------------------------------
# Plugin Loader
# ---------------------------------------------------------------------------


class PluginLoader:
    """Discovers, validates, and loads plugins into the runtime."""

    def __init__(
        self,
        registry: PluginRegistryService | None = None,
        plugin_dir: Path | None = None,
    ) -> None:
        self._registry = registry or PluginRegistryService()
        self._plugin_dir = plugin_dir or self._registry.plugin_dir
        self._loaded: dict[str, PluginStatus] = {}
        self._specs: list[PluginSpec] = []

        # Read lenient_builtins from workspace config
        self._lenient_builtins = True
        try:
            from obscura.core.workspace import load_workspace_config
            config = load_workspace_config()
            self._lenient_builtins = (
                config.get("plugins", {})
                .get("bootstrap", {})
                .get("lenient_builtins", True)
            )
        except Exception:
            pass  # default to lenient

    # -- Discovery ---------------------------------------------------------

    def discover_builtins(self) -> list[PluginSpec]:
        """Discover built-in plugin manifests shipped with Obscura."""
        from obscura.plugins.builtins import list_builtin_manifests

        specs: list[PluginSpec] = []
        for path in list_builtin_manifests():
            try:
                spec = parse_manifest_file(path)
                specs.append(spec)
            except ManifestError as exc:
                logger.warning("Skipping invalid builtin manifest %s: %s", path, exc)
        return specs

    def discover_local(self) -> list[PluginSpec]:
        """Discover plugins from the local plugins directory that have manifests."""
        return self._discover_from_dir(self._plugin_dir)

    def discover_user(self) -> list[PluginSpec]:
        """Discover user-authored plugins from the global ``~/.obscura/plugins/``.

        Always scans the global home regardless of whether a local ``.obscura/``
        exists. Supports both flat YAML manifests (like builtins) and subdirectory
        layouts (``subdir/plugin.yaml``).

        If the active plugin dir already IS the global dir (no local override),
        returns an empty list to avoid double-loading.
        """
        global_plugins = resolve_obscura_global_home() / "plugins"
        if global_plugins == self._plugin_dir:
            # Already covered by discover_local()
            return []
        return self._discover_from_dir(global_plugins)

    @staticmethod
    def _discover_from_dir(directory: Path) -> list[PluginSpec]:
        """Discover plugin manifests from a directory.

        Supports two layouts:
        - **Flat YAML**: ``directory/*.yaml`` (like builtins)
        - **Subdirectory**: ``directory/<name>/plugin.yaml`` or ``plugin.json``

        Skips ``registry.json`` and non-manifest files.
        """
        specs: list[PluginSpec] = []
        if not directory.exists():
            return specs
        for entry in sorted(directory.iterdir()):
            if entry.is_dir():
                # Subdirectory layout: <name>/plugin.yaml
                manifest = entry / "plugin.yaml"
                if not manifest.exists():
                    manifest = entry / "plugin.json"
                if manifest.exists():
                    try:
                        spec = parse_manifest_file(manifest)
                        specs.append(spec)
                    except ManifestError as exc:
                        logger.warning("Skipping invalid manifest %s: %s", manifest, exc)
            elif entry.suffix == ".yaml" and entry.name != "registry.json":
                # Flat YAML layout (like builtins): *.yaml
                try:
                    spec = parse_manifest_file(entry)
                    specs.append(spec)
                except ManifestError as exc:
                    logger.debug("Skipping non-manifest YAML %s: %s", entry, exc)
        return specs

    # -- Loading pipeline --------------------------------------------------

    def _load_spec(
        self,
        spec: PluginSpec,
        provider_registry: Any,
    ) -> PluginStatus:
        """Run the full loading pipeline for a single PluginSpec.

        Pipeline: validate → check_config → bootstrap → create_provider → register

        Respects ``plugins.bootstrap.lenient_builtins`` from workspace config.yaml.
        When lenient (default), builtin plugins whose config or bootstrap fails
        still get their tools registered.
        """
        status = PluginStatus(plugin_id=spec.id, state="discovered")

        # Determine if this builtin should be treated leniently
        lenient = spec.source_type == "builtin" and self._lenient_builtins

        # 1. Validate
        errors = validate_plugin_spec(spec)
        hard_errors = [e for e in errors if e.severity == "error"]
        if hard_errors:
            status.state = "failed"
            status.error = "; ".join(str(e) for e in hard_errors)
            logger.warning("Plugin %s failed validation: %s", spec.id, status.error)
            return status

        # 2. Check config — lenient builtins warn but still register tools
        config_ok, missing = _check_config(spec)
        if not config_ok:
            if lenient:
                logger.info(
                    "Plugin %s missing config (%s) — tools registered but may fail at runtime",
                    spec.id, ", ".join(missing),
                )
            else:
                status.state = "disabled"
                status.error = f"Missing config: {', '.join(missing)}"
                logger.info("Plugin %s disabled (missing config: %s)", spec.id, missing)
                return status

        # 3. Bootstrap dependencies — lenient builtins warn but still register tools
        if spec.bootstrap and spec.bootstrap.deps:
            try:
                from obscura.plugins.bootstrapper import run_bootstrap

                bootstrap_result = run_bootstrap(spec)
                if not bootstrap_result.ok:
                    if lenient:
                        logger.info(
                            "Plugin %s bootstrap incomplete (%s) — tools registered but may fail at runtime",
                            spec.id, "; ".join(bootstrap_result.errors),
                        )
                    else:
                        status.state = "failed"
                        status.error = f"Bootstrap failed: {'; '.join(bootstrap_result.errors)}"
                        logger.warning("Plugin %s bootstrap failed: %s", spec.id, bootstrap_result.errors)
                        return status
                if bootstrap_result.installed:
                    logger.info(
                        "Plugin %s bootstrapped: installed %s",
                        spec.id, ", ".join(bootstrap_result.installed),
                    )
            except Exception as exc:
                if lenient:
                    logger.info("Plugin %s bootstrap skipped: %s — tools still registered", spec.id, exc)
                else:
                    status.state = "failed"
                    status.error = f"Bootstrap error: {exc}"
                    logger.exception("Plugin %s bootstrap error: %s", spec.id, exc)
                    return status

        # 4. Create provider
        try:
            provider = ManifestToolProvider(spec)
            provider_registry.add(provider)
            status.state = "enabled"
            status.enabled = True
            self._specs.append(spec)
        except Exception as exc:
            status.state = "failed"
            status.error = str(exc)
            logger.exception("Plugin %s failed to create provider: %s", spec.id, exc)

        return status

    def load_builtins(self, provider_registry: Any) -> dict[str, PluginStatus]:
        """Load all built-in plugins."""
        results: dict[str, PluginStatus] = {}
        for spec in self.discover_builtins():
            status = self._load_spec(spec, provider_registry)
            self._loaded[spec.id] = status
            results[spec.id] = status
        return results

    def load_local(self, provider_registry: Any) -> dict[str, PluginStatus]:
        """Load manifest-based plugins from the local plugins directory."""
        results: dict[str, PluginStatus] = {}
        for spec in self.discover_local():
            status = self._load_spec(spec, provider_registry)
            self._loaded[spec.id] = status
            results[spec.id] = status
        return results

    def load_user(self, provider_registry: Any) -> dict[str, PluginStatus]:
        """Load user-authored plugins from global ``~/.obscura/plugins/``."""
        results: dict[str, PluginStatus] = {}
        for spec in self.discover_user():
            if spec.id in self._loaded:
                logger.debug("Skipping user plugin %s (already loaded)", spec.id)
                continue
            status = self._load_spec(spec, provider_registry)
            self._loaded[spec.id] = status
            results[spec.id] = status
        return results

    # -- Main entry point --------------------------------------------------

    def load_all(self, provider_registry: Any) -> dict[str, Any]:
        """Load all plugins from all sources.

        Respects ``plugins.load_builtins`` from workspace config.yaml.
        Returns a summary dict with counts and statuses.
        """
        results: dict[str, Any] = {
            "builtins": {},
            "local_manifest": {},
            "user_plugins": {},
        }

        # Check config for load_builtins setting
        load_builtins = True
        try:
            from obscura.core.workspace import load_workspace_config
            config = load_workspace_config()
            load_builtins = config.get("plugins", {}).get("load_builtins", True)
        except Exception:
            pass

        # 1. Builtins (manifest-based)
        if load_builtins:
            results["builtins"] = self.load_builtins(provider_registry)
        else:
            logger.info("Builtin plugins disabled in config.yaml")

        # 2. Local manifest-based plugins (project .obscura/plugins/)
        results["local_manifest"] = self.load_local(provider_registry)

        # 3. User plugins from global ~/.obscura/plugins/ (flat YAML + subdirs)
        results["user_plugins"] = self.load_user(provider_registry)

        enabled = sum(1 for s in self._loaded.values() if s.enabled)
        total = len(self._loaded)
        logger.info("Plugin loader: %d/%d plugins enabled", enabled, total)

        return results

    def load_all_enabled(self, provider_registry: Any) -> dict[str, Any]:
        """Convenience alias for ``load_all``."""
        return self.load_all(provider_registry)

    def load_scoped(
        self,
        provider_registry: Any,
        required_ids: list[str],
        optional_ids: list[str],
    ) -> dict[str, PluginStatus]:
        """Load only the plugins listed in *required_ids* / *optional_ids*.

        Discovers all specs (builtins + local), filters to those whose
        ``spec.id`` appears in either list, and runs ``_load_spec`` on each.

        Raises ``RuntimeError`` if any *required* plugin is not found or
        fails to reach the ``enabled`` state.
        """
        all_specs = self.discover_builtins() + self.discover_local() + self.discover_user()
        wanted = set(required_ids) | set(optional_ids)
        spec_map: dict[str, PluginSpec] = {}
        for spec in all_specs:
            if spec.id in wanted:
                spec_map[spec.id] = spec

        results: dict[str, PluginStatus] = {}

        for pid in required_ids:
            spec = spec_map.get(pid)
            if spec is None:
                status = PluginStatus(
                    plugin_id=pid, state="failed", error="not found",
                )
                results[pid] = status
                self._loaded[pid] = status
                continue
            status = self._load_spec(spec, provider_registry)
            results[pid] = status
            self._loaded[pid] = status

        for pid in optional_ids:
            spec = spec_map.get(pid)
            if spec is None:
                logger.warning("Optional plugin %s not found — skipping", pid)
                continue
            status = self._load_spec(spec, provider_registry)
            results[pid] = status
            self._loaded[pid] = status
            if status.state != "enabled":
                logger.warning(
                    "Optional plugin %s failed to load: %s", pid, status.error,
                )

        failed_required = [
            pid for pid in required_ids
            if pid in results and results[pid].state != "enabled"
        ]
        if failed_required:
            details = "; ".join(
                f"{pid}: {results[pid].error}" for pid in failed_required
            )
            raise RuntimeError(
                f"Required plugins failed to load: {details}"
            )

        return results

    # -- Status queries ----------------------------------------------------

    def get_status(self, plugin_id: str) -> PluginStatus | None:
        return self._loaded.get(plugin_id)

    def list_loaded(self) -> dict[str, PluginStatus]:
        return dict(self._loaded)

    @property
    def loaded_specs(self) -> list[PluginSpec]:
        """Return a copy of all successfully loaded PluginSpecs."""
        return list(self._specs)


def get_all_builtin_tool_specs() -> list[Any]:
    """Resolve all builtin and user plugin tools into ToolSpec instances.

    Convenience function for the CLI and other non-Agent code paths that need
    plugin tools without the full provider/context pipeline.  Respects the
    ``plugins.load_builtins`` setting from workspace config.yaml.  Returns a
    list of ``ToolSpec`` instances with resolved handlers.  Tools whose handler
    cannot be resolved are silently skipped.
    """
    from obscura.core.types import ToolSpec

    # Respect config.yaml plugins.load_builtins setting
    load_builtins = True
    try:
        from obscura.core.workspace import load_workspace_config
        config = load_workspace_config()
        load_builtins = config.get("plugins", {}).get("load_builtins", True)
    except Exception:
        pass  # fallback: load builtins anyway

    loader = PluginLoader()
    all_plugin_specs: list[PluginSpec] = []
    if load_builtins:
        all_plugin_specs.extend(loader.discover_builtins())
    all_plugin_specs.extend(loader.discover_local())
    all_plugin_specs.extend(loader.discover_user())

    specs: list[Any] = []
    for plugin_spec in all_plugin_specs:
        for tool in plugin_spec.tools:
            handler = _resolve_handler(tool.handler_ref)
            if handler is None:
                logger.debug(
                    "Skipping unresolvable tool %s (%s)",
                    tool.name, tool.handler_ref,
                )
                continue
            specs.append(ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
                handler=handler,
                side_effects=tool.side_effects,
                required_tier=tool.required_tier,
                timeout_seconds=tool.timeout_seconds,
                retries=tool.retries,
            ))
    return specs


__all__ = [
    "PluginLoader",
    "ManifestToolProvider",
    "get_all_builtin_tool_specs",
]
