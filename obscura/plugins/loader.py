"""Plugin loader pipeline for Obscura.

Structured pipeline that replaces the earlier ``plugin_loader.py``:

    discover → validate_manifest → resolve_config → bootstrap →
    normalize_resources → register → track_health

Lifecycle states: discovered → installed → enabled → active → unhealthy → disabled → failed

The loader works with the ``PluginRegistryService`` for persistence and
the ``ToolBroker`` for runtime tool registration.

Usage::

    from obscura.plugins.loader import PluginLoader

    loader = PluginLoader()
    loader.load_all_enabled(broker)
    loader.load_builtins(broker)
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import sys
from typing import TYPE_CHECKING, Any, cast

from obscura.core.paths import resolve_obscura_global_home
from obscura.core.types import ToolSpec
from obscura.plugins.builtins import list_builtin_manifests
from obscura.plugins.lazy import LazyPluginManager
from obscura.plugins.manifest import ManifestError, parse_manifest_file
from obscura.plugins.models import (
    PluginSpec,
    PluginStatus,
)
from obscura.plugins.registry import PluginRegistryService
from obscura.plugins.validator import validate_plugin_spec

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workspace config helpers
# ---------------------------------------------------------------------------


def _load_plugin_config_flag(key: str, default: bool = True) -> bool:
    """Read a boolean flag from workspace config ``plugins`` section.

    Supports dotted *key* like ``"load_builtins"`` or ``"bootstrap.lenient_builtins"``.
    Returns *default* on any failure.
    """
    try:
        from obscura.core.workspace import load_workspace_config

        config = load_workspace_config()
        section: Any = config.get("plugins", {})
        for part in key.split("."):
            if not isinstance(section, dict):
                return default
            section = cast(dict[str, Any], section).get(part, {})
        return section if isinstance(section, bool) else default
    except Exception:
        logger.debug("suppressed exception in _load_plugin_config_flag", exc_info=True)
        return default


def _load_plugin_config_list(key: str) -> frozenset[str]:
    """Read a list of plugin IDs from workspace config ``plugins`` section.

    Supports keys like ``"include"`` or ``"exclude"`` under ``[plugins]``.
    Returns an empty frozenset on any failure or if the key is absent.
    """
    try:
        from obscura.core.workspace import load_workspace_config

        config = load_workspace_config()
        section: dict[str, Any] = config.get("plugins", {})
        val = section.get(key)
        if isinstance(val, list):
            items = cast(list[Any], val)
            return frozenset(str(v) for v in items)
        return frozenset()
    except Exception:
        logger.debug("suppressed exception in _load_plugin_config_list", exc_info=True)
        return frozenset()


def _apply_plugin_filters(specs: list[PluginSpec]) -> list[PluginSpec]:
    """Filter plugin specs using ``plugins.include`` / ``plugins.exclude`` from config.toml.

    * If ``include`` is non-empty, only plugins whose ``id`` is in the set are kept.
    * Plugins whose ``id`` is in ``exclude`` are always removed.
    * Local and user plugins are never filtered out by ``include`` (only builtins).
    """
    include = _load_plugin_config_list("include")
    exclude = _load_plugin_config_list("exclude")
    if not include and not exclude:
        return specs
    filtered: list[PluginSpec] = []
    for s in specs:
        if exclude and s.id in exclude:
            continue
        if include and s.id not in include:
            continue
        filtered.append(s)
    return filtered


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


# Runtime types whose tools are served externally (not via Python handlers).
_EXTERNAL_RUNTIME_TYPES = frozenset({"mcp", "grpc", "docker", "service"})


def _resolve_handler_from_plugin_module(
    tool_name: str,
    plugin_spec: Any,
) -> Any | None:
    """Resolve a tool handler from a plugin's ``bootstrap.tools_module``.

    When a plugin declares ``tools_module`` (e.g. ``"tools.tools"``) and
    ``tools_list`` (e.g. ``"TOOL_SPECS"``), we import that module relative
    to the plugin's source directory and look up the tool by name.

    Returns the callable handler or ``None`` on failure.
    """
    bootstrap = getattr(plugin_spec, "bootstrap", None)
    if not bootstrap:
        return None
    tools_module = getattr(bootstrap, "tools_module", "")
    if not tools_module:
        return None
    source_dir = getattr(plugin_spec, "source_dir", None)
    if not source_dir:
        return None

    source_str = str(source_dir)
    added = source_str not in sys.path
    if added:
        sys.path.insert(0, source_str)

    # Evict cached modules so each plugin gets a fresh import.
    # Multiple plugins may use the same relative module name (e.g. "tools.tools").
    parts = tools_module.split(".")
    evicted: dict[str, Any] = {}
    for i in range(len(parts)):
        key = ".".join(parts[: i + 1])
        if key in sys.modules:
            evicted[key] = sys.modules.pop(key)

    try:
        mod = importlib.import_module(tools_module)
        # First try: tool_name is a module-level function
        handler = getattr(mod, tool_name, None)
        if handler is not None:
            return handler
        # Second try: tools_list is a dict mapping name → handler
        tools_list_attr = getattr(bootstrap, "tools_list", "")
        if tools_list_attr:
            spec_map = getattr(mod, tools_list_attr, None)
            if isinstance(spec_map, dict) and tool_name in spec_map:
                return cast(dict[str, Any], spec_map)[tool_name]
        return None
    except Exception as exc:
        logger.debug(
            "Failed to resolve tool %s from plugin module %s: %s",
            tool_name,
            tools_module,
            exc,
        )
        return None
    finally:
        # Remove freshly imported modules and restore previous state
        for i in range(len(parts)):
            key = ".".join(parts[: i + 1])
            sys.modules.pop(key, None)
        sys.modules.update(evicted)
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(source_str)


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

        self._lenient_builtins = _load_plugin_config_flag("bootstrap.lenient_builtins")

    # -- Discovery ---------------------------------------------------------

    def discover_builtins(self) -> list[PluginSpec]:
        """Discover built-in plugin manifests shipped with Obscura."""
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
        - **Flat**: ``directory/*.toml`` or ``directory/*.yaml``
        - **Subdirectory**: ``directory/<name>/plugin.toml``, ``plugin.yaml``, or ``plugin.json``

        TOML is preferred over YAML when both exist.
        Skips ``registry.json`` and non-manifest files.
        """
        specs: list[PluginSpec] = []
        if not directory.exists():
            return specs
        for entry in sorted(directory.iterdir()):
            if entry.is_dir():
                # Subdirectory layout: <name>/plugin.toml or plugin.yaml
                manifest: Path | None = None
                for fname in ("plugin.toml", "plugin.yaml", "plugin.json"):
                    candidate = entry / fname
                    if candidate.exists():
                        manifest = candidate
                        break
                if manifest is not None:
                    try:
                        spec = parse_manifest_file(manifest)
                        specs.append(spec)
                    except ManifestError as exc:
                        logger.warning(
                            "Skipping invalid manifest %s: %s",
                            manifest,
                            exc,
                        )
            elif entry.suffix in (".toml", ".yaml") and entry.name != "registry.json":
                # Flat layout: *.toml or *.yaml
                try:
                    spec = parse_manifest_file(entry)
                    specs.append(spec)
                except ManifestError as exc:
                    logger.debug("Skipping non-manifest file %s: %s", entry, exc)
        return specs

    # -- Loading pipeline --------------------------------------------------

    def _load_spec(
        self,
        spec: PluginSpec,
        broker: Any,
    ) -> PluginStatus:
        """Run the full loading pipeline for a single PluginSpec.

        Pipeline: validate → check_config → bootstrap → create_provider → register

        Respects ``plugins.bootstrap.lenient_builtins`` from workspace config.toml.
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
                    spec.id,
                    ", ".join(missing),
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
                            spec.id,
                            "; ".join(bootstrap_result.errors),
                        )
                    else:
                        status.state = "failed"
                        status.error = (
                            f"Bootstrap failed: {'; '.join(bootstrap_result.errors)}"
                        )
                        logger.warning(
                            "Plugin %s bootstrap failed: %s",
                            spec.id,
                            bootstrap_result.errors,
                        )
                        return status
                if bootstrap_result.installed:
                    logger.info(
                        "Plugin %s bootstrapped: installed %s",
                        spec.id,
                        ", ".join(bootstrap_result.installed),
                    )
            except Exception as exc:
                if lenient:
                    logger.info(
                        "Plugin %s bootstrap skipped: %s — tools still registered",
                        spec.id,
                        exc,
                    )
                else:
                    status.state = "failed"
                    status.error = f"Bootstrap error: {exc}"
                    logger.exception("Plugin %s bootstrap error: %s", spec.id, exc)
                    return status

        # 4a. Runtime override: expose native plugin as MCP server instead.
        try:
            from obscura.plugins.runtime_adapter import (
                RUNTIME_MCP,
                get_runtime_override,
                write_mcp_config_for_plugin,
            )

            if get_runtime_override(spec.id) == RUNTIME_MCP:
                # Resolve handlers first, then wrap as MCP.
                from obscura.core.types import ToolSpec

                native_tools: list[ToolSpec] = []
                for tc in spec.tools:
                    handler = _resolve_handler(tc.handler_ref)
                    if handler is None:
                        handler = _resolve_handler_from_plugin_module(tc.name, spec)
                    if handler is not None:
                        native_tools.append(
                            ToolSpec(
                                name=tc.name,
                                description=tc.description,
                                parameters=tc.parameters,
                                handler=handler,
                            )
                        )
                if native_tools:
                    config_path = write_mcp_config_for_plugin(spec.id, native_tools)
                    if config_path:
                        logger.info(
                            "Plugin %s runtime overridden to MCP (%d tools) → %s",
                            spec.id,
                            len(native_tools),
                            config_path,
                        )
                status.state = "enabled"
                status.enabled = True
                self._specs.append(spec)
                return status
        except Exception:
            logger.debug("suppressed exception in _load_spec", exc_info=True)

        # 4b. Register tools directly on broker
        #     MCP/service/docker/grpc plugins serve tools externally — skip.
        if spec.runtime_type in _EXTERNAL_RUNTIME_TYPES:
            status.state = "enabled"
            status.enabled = True
            self._specs.append(spec)
            logger.info(
                "Plugin %s (%s) loaded: %d tools declared (served externally)",
                spec.id,
                spec.runtime_type,
                len(spec.tools),
            )
            return status

        try:
            from obscura.core.types import ToolSpec

            registered_count = 0
            for tool_contrib in spec.tools:
                handler = _resolve_handler(tool_contrib.handler_ref)
                if handler is None:
                    handler = _resolve_handler_from_plugin_module(
                        tool_contrib.name,
                        spec,
                    )
                if handler is None:
                    logger.warning(
                        "Plugin %s: could not resolve handler for tool %s (%s)",
                        spec.id,
                        tool_contrib.name,
                        tool_contrib.handler_ref,
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
                    capability=tool_contrib.capability,
                )
                broker.register_tool_spec(tool_spec)
                registered_count += 1

            status.state = "enabled"
            status.enabled = True
            self._specs.append(spec)
            logger.info(
                "Plugin %s loaded: %d tools registered",
                spec.id,
                registered_count,
            )
        except Exception as exc:
            status.state = "failed"
            status.error = str(exc)
            logger.exception("Plugin %s failed to register tools: %s", spec.id, exc)

        return status

    def load_builtins(self, broker: Any) -> dict[str, PluginStatus]:
        """Load all built-in plugins, respecting ``plugins.include``/``exclude``."""
        results: dict[str, PluginStatus] = {}
        for spec in _apply_plugin_filters(list(self.discover_builtins())):
            status = self._load_spec(spec, broker)
            self._loaded[spec.id] = status
            results[spec.id] = status
        return results

    def load_local(self, broker: Any) -> dict[str, PluginStatus]:
        """Load manifest-based plugins from the local plugins directory."""
        results: dict[str, PluginStatus] = {}
        for spec in self.discover_local():
            status = self._load_spec(spec, broker)
            self._loaded[spec.id] = status
            results[spec.id] = status
        return results

    def load_user(self, broker: Any) -> dict[str, PluginStatus]:
        """Load user-authored plugins from global ``~/.obscura/plugins/``."""
        results: dict[str, PluginStatus] = {}
        for spec in self.discover_user():
            if spec.id in self._loaded:
                logger.debug("Skipping user plugin %s (already loaded)", spec.id)
                continue
            status = self._load_spec(spec, broker)
            self._loaded[spec.id] = status
            results[spec.id] = status
        return results

    # -- Main entry point --------------------------------------------------

    def load_all(self, broker: Any) -> dict[str, Any]:
        """Load all plugins from all sources.

        Respects ``plugins.load_builtins`` from workspace config.toml.
        Returns a summary dict with counts and statuses.
        """
        results: dict[str, Any] = {
            "builtins": {},
            "local_manifest": {},
            "user_plugins": {},
        }

        load_builtins = _load_plugin_config_flag("load_builtins")

        # 1. Builtins (manifest-based)
        if load_builtins:
            results["builtins"] = self.load_builtins(broker)
        else:
            logger.info("Builtin plugins disabled in config.toml")

        # 2. Local manifest-based plugins (project .obscura/plugins/)
        results["local_manifest"] = self.load_local(broker)

        # 3. User plugins from global ~/.obscura/plugins/ (flat YAML + subdirs)
        results["user_plugins"] = self.load_user(broker)

        enabled = sum(1 for s in self._loaded.values() if s.enabled)
        total = len(self._loaded)
        logger.info("Plugin loader: %d/%d plugins enabled", enabled, total)

        return results

    def load_all_enabled(self, broker: Any) -> dict[str, Any]:
        """Convenience alias for ``load_all``."""
        return self.load_all(broker)

    def load_lazy(
        self,
        broker: Any,
        *,
        plugin_include: frozenset[str] | None = None,
        plugin_exclude: frozenset[str] | None = None,
        eager_plugins: set[str] | None = None,
    ) -> Any:
        """Discover all plugins but defer initialization until first tool use.

        Returns a :class:`LazyPluginManager` that is wired into the *broker*
        via ``broker.set_lazy_manager()``.  Only tool schemas (name,
        description, parameters) are registered eagerly so the LLM can see
        them; actual handler modules are imported on first call.

        Parameters
        ----------
        broker : ToolBroker
            The broker that will receive handler registrations on lazy init.
        plugin_include / plugin_exclude :
            Pack-level filtering (same semantics as workspace filters).
        eager_plugins :
            Plugin IDs to initialize immediately (prewarm set).

        """
        load_builtins = _load_plugin_config_flag("load_builtins")

        all_specs: list[PluginSpec] = []
        if load_builtins:
            all_specs.extend(self.discover_builtins())
        all_specs.extend(self.discover_local())
        all_specs.extend(self.discover_user())

        # Apply config.toml-level filtering first
        all_specs = _apply_plugin_filters(all_specs)

        # Apply pack-based filtering (from compiled workspace specs)
        if plugin_include:
            all_specs = [s for s in all_specs if s.id in plugin_include]
        if plugin_exclude:
            all_specs = [s for s in all_specs if s.id not in plugin_exclude]

        # Filter out external runtimes (MCP/gRPC/Docker served externally)
        all_specs = [
            s for s in all_specs if s.runtime_type not in _EXTERNAL_RUNTIME_TYPES
        ]

        def _init_plugin(spec: PluginSpec) -> None:
            """Fully initialize a plugin: resolve handlers, register on broker."""
            self._load_spec(spec, broker)

        manager = LazyPluginManager(
            init_fn=_init_plugin,
            prewarm=eager_plugins or set(),
        )

        # Register all discovered plugins (metadata + schemas only)
        for spec in all_specs:
            manager.register(spec)

            # Register tool schemas on broker so the LLM can see them.
            # No handler is registered — the lazy manager will resolve it
            # on first call via broker's lazy resolution path.
            for tool in spec.tools:
                if not broker._specs.get(tool.name):
                    if tool.parameters:
                        broker._schemas[tool.name] = tool.parameters

                    def _placeholder_handler(**_kw: Any) -> None:
                        return None

                    broker._specs[tool.name] = ToolSpec(
                        name=tool.name,
                        description=tool.description,
                        parameters=tool.parameters,
                        handler=_placeholder_handler,  # never called; lazy resolution replaces it
                        side_effects=tool.side_effects,
                        required_tier=tool.required_tier,
                        timeout_seconds=tool.timeout_seconds,
                        retries=tool.retries,
                        capability=tool.capability,
                    )

        # Wire the manager into the broker for on-demand resolution
        broker.set_lazy_manager(manager)

        eager_set: set[str] = eager_plugins or set()
        logger.info(
            "Lazy plugin loader: %d plugins discovered, %d eager, %d lazy",
            len(all_specs),
            len(eager_set),
            len(all_specs) - len(eager_set),
        )
        return manager

    def load_scoped(
        self,
        broker: Any,
        required_ids: list[str],
        optional_ids: list[str],
    ) -> dict[str, PluginStatus]:
        """Load only the plugins listed in *required_ids* / *optional_ids*.

        Discovers all specs (builtins + local), filters to those whose
        ``spec.id`` appears in either list, and runs ``_load_spec`` on each.

        Raises ``RuntimeError`` if any *required* plugin is not found or
        fails to reach the ``enabled`` state.
        """
        all_specs = (
            self.discover_builtins() + self.discover_local() + self.discover_user()
        )
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
                    plugin_id=pid,
                    state="failed",
                    error="not found",
                )
                results[pid] = status
                self._loaded[pid] = status
                continue
            status = self._load_spec(spec, broker)
            results[pid] = status
            self._loaded[pid] = status

        for pid in optional_ids:
            spec = spec_map.get(pid)
            if spec is None:
                logger.warning("Optional plugin %s not found — skipping", pid)
                continue
            status = self._load_spec(spec, broker)
            results[pid] = status
            self._loaded[pid] = status
            if status.state != "enabled":
                logger.warning(
                    "Optional plugin %s failed to load: %s",
                    pid,
                    status.error,
                )

        failed_required = [
            pid
            for pid in required_ids
            if pid in results and results[pid].state != "enabled"
        ]
        if failed_required:
            details = "; ".join(
                f"{pid}: {results[pid].error}" for pid in failed_required
            )
            msg = f"Required plugins failed to load: {details}"
            raise RuntimeError(msg)

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
    ``plugins.load_builtins`` setting from workspace config.toml.  Returns a
    list of ``ToolSpec`` instances with resolved handlers.  Tools whose handler
    cannot be resolved are silently skipped.
    """
    load_builtins = _load_plugin_config_flag("load_builtins")

    loader = PluginLoader()
    all_plugin_specs: list[PluginSpec] = []
    if load_builtins:
        all_plugin_specs.extend(loader.discover_builtins())
    all_plugin_specs.extend(loader.discover_local())
    all_plugin_specs.extend(loader.discover_user())
    all_plugin_specs = _apply_plugin_filters(all_plugin_specs)

    specs: list[Any] = []
    for plugin_spec in all_plugin_specs:
        if plugin_spec.runtime_type in _EXTERNAL_RUNTIME_TYPES:
            continue
        for tool in plugin_spec.tools:
            handler = _resolve_handler(tool.handler_ref)
            if handler is None:
                handler = _resolve_handler_from_plugin_module(tool.name, plugin_spec)
            if handler is None:
                logger.debug(
                    "Skipping unresolvable tool %s (%s)",
                    tool.name,
                    tool.handler_ref,
                )
                continue
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    handler=handler,
                    side_effects=tool.side_effects,
                    required_tier=tool.required_tier,
                    timeout_seconds=tool.timeout_seconds,
                    retries=tool.retries,
                    capability=tool.capability,
                ),
            )
    return specs


def get_filtered_builtin_tool_specs(
    plugin_include: frozenset[str] | None = None,
    plugin_exclude: frozenset[str] | None = None,
) -> list[Any]:
    """Resolve builtin/user plugin tools, filtered by include/exclude sets.

    Like :func:`get_all_builtin_tool_specs` but applies pack-based filtering:

    * If *plugin_include* is non-empty, only plugins whose ``id`` is in the
      set are kept.
    * Plugins whose ``id`` is in *plugin_exclude* are always removed.

    This enables pack-scoped sessions where only a subset of available
    plugins (and therefore tools) are loaded into the model context.
    """
    load_builtins = _load_plugin_config_flag("load_builtins")

    loader = PluginLoader()
    all_plugin_specs: list[PluginSpec] = []
    if load_builtins:
        all_plugin_specs.extend(loader.discover_builtins())
    all_plugin_specs.extend(loader.discover_local())
    all_plugin_specs.extend(loader.discover_user())

    # Apply pack-based filtering
    if plugin_include:
        all_plugin_specs = [s for s in all_plugin_specs if s.id in plugin_include]
    if plugin_exclude:
        all_plugin_specs = [s for s in all_plugin_specs if s.id not in plugin_exclude]

    specs: list[Any] = []
    for plugin_spec in all_plugin_specs:
        if plugin_spec.runtime_type in _EXTERNAL_RUNTIME_TYPES:
            continue
        for tool in plugin_spec.tools:
            handler = _resolve_handler(tool.handler_ref)
            if handler is None:
                handler = _resolve_handler_from_plugin_module(tool.name, plugin_spec)
            if handler is None:
                logger.debug(
                    "Skipping unresolvable tool %s (%s)",
                    tool.name,
                    tool.handler_ref,
                )
                continue
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    handler=handler,
                    side_effects=tool.side_effects,
                    required_tier=tool.required_tier,
                    timeout_seconds=tool.timeout_seconds,
                    retries=tool.retries,
                    capability=tool.capability,
                ),
            )

    logger.info(
        "Filtered plugin tools: %d plugins → %d tools (include=%s, exclude=%s)",
        len(all_plugin_specs),
        len(specs),
        plugin_include or "all",
        plugin_exclude or "none",
    )
    return specs


def get_all_builtin_tool_specs_with_report() -> tuple[list[Any], list[tuple[str, str]]]:
    """Resolve builtin/user plugin tools, reporting skipped tools.

    Returns ``(resolved_specs, skipped_tools)`` where *skipped_tools* is a
    list of ``(tool_name, handler_ref)`` tuples for tools whose handler
    could not be resolved.
    """
    load_builtins = _load_plugin_config_flag("load_builtins")

    loader = PluginLoader()
    all_plugin_specs: list[PluginSpec] = []
    if load_builtins:
        all_plugin_specs.extend(loader.discover_builtins())
    all_plugin_specs.extend(loader.discover_local())
    all_plugin_specs.extend(loader.discover_user())
    all_plugin_specs = _apply_plugin_filters(all_plugin_specs)

    specs: list[Any] = []
    skipped: list[tuple[str, str]] = []
    for plugin_spec in all_plugin_specs:
        if plugin_spec.runtime_type in _EXTERNAL_RUNTIME_TYPES:
            continue
        for tool in plugin_spec.tools:
            handler = _resolve_handler(tool.handler_ref)
            if handler is None:
                handler = _resolve_handler_from_plugin_module(tool.name, plugin_spec)
            if handler is None:
                logger.debug(
                    "Skipping unresolvable tool %s (%s)",
                    tool.name,
                    tool.handler_ref,
                )
                skipped.append((tool.name, tool.handler_ref))
                continue
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    handler=handler,
                    side_effects=tool.side_effects,
                    required_tier=tool.required_tier,
                    timeout_seconds=tool.timeout_seconds,
                    retries=tool.retries,
                    capability=tool.capability,
                ),
            )
    return specs, skipped


def get_capability_map() -> dict[str, str]:
    """Return ``{tool_name: capability_id}`` from all loaded plugin manifests.

    Scans all discoverable plugin specs (builtin, local, user) and builds a
    reverse lookup from tool name to the capability it belongs to.  This is
    used to backfill the ``capability`` field on system tools that are
    registered via the ``@tool()`` decorator (which has no capability param).
    """
    load_builtins = _load_plugin_config_flag("load_builtins")

    loader = PluginLoader()
    all_plugin_specs: list[PluginSpec] = []
    if load_builtins:
        all_plugin_specs.extend(loader.discover_builtins())
    all_plugin_specs.extend(loader.discover_local())
    all_plugin_specs.extend(loader.discover_user())
    all_plugin_specs = _apply_plugin_filters(all_plugin_specs)

    cap_map: dict[str, str] = {}
    for plugin_spec in all_plugin_specs:
        for tool in plugin_spec.tools:
            if tool.capability:
                cap_map[tool.name] = tool.capability
    return cap_map


__all__ = [
    "PluginLoader",
    "get_all_builtin_tool_specs",
    "get_all_builtin_tool_specs_with_report",
    "get_capability_map",
    "get_filtered_builtin_tool_specs",
]
