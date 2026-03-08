"""Plugin registry service for Obscura.

Manages the persistent state of installed plugins: what is available,
what version is installed, what is enabled, and what resources each
plugin contributes.

State is stored in ``~/.obscura/plugins/registry.json`` (upgradeable to
SQLite later). The service exposes clean interfaces for list/get/install/
enable/disable/uninstall operations.

Usage::

    from obscura.plugins.registry import PluginRegistryService

    svc = PluginRegistryService()
    svc.install(spec)
    svc.enable("obscura-github")
    for entry in svc.list_plugins():
        print(entry)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obscura.core.paths import resolve_obscura_home
from obscura.plugins.models import PluginSpec, PluginStatus, TRUST_LEVELS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry entry (serializable to JSON)
# ---------------------------------------------------------------------------


@dataclass
class PluginEntry:
    """A single plugin record in the registry."""

    id: str
    name: str
    version: str
    source_type: str
    runtime_type: str
    trust_level: str
    author: str
    description: str
    source: str                              # install source (path, URL, package name)
    enabled: bool = False
    state: str = "installed"                 # lifecycle state
    error: str | None = None
    installed_at: str = ""
    updated_at: str = ""
    contributed_capabilities: list[str] = field(default_factory=list)
    contributed_tools: list[str] = field(default_factory=list)
    contributed_workflows: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginEntry:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    @classmethod
    def from_spec(cls, spec: PluginSpec, source: str = "") -> PluginEntry:
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            id=spec.id,
            name=spec.name,
            version=spec.version,
            source_type=spec.source_type,
            runtime_type=spec.runtime_type,
            trust_level=spec.trust_level,
            author=spec.author,
            description=spec.description,
            source=source,
            enabled=False,
            state="installed",
            installed_at=now,
            updated_at=now,
            contributed_capabilities=list(spec.capability_ids),
            contributed_tools=list(spec.tool_names),
            contributed_workflows=list(spec.workflow_ids),
        )


# ---------------------------------------------------------------------------
# Registry service
# ---------------------------------------------------------------------------


class PluginRegistryService:
    """Manages the persistent plugin registry under ``~/.obscura/plugins/``."""

    def __init__(self, plugin_dir: Path | None = None) -> None:
        self._home = resolve_obscura_home()
        self._plugin_dir = plugin_dir or (self._home / "plugins")
        self._plugin_dir.mkdir(parents=True, exist_ok=True)
        self._registry_file = self._plugin_dir / "registry.json"
        if not self._registry_file.exists():
            self._write([])

    # -- persistence -------------------------------------------------------

    def _read(self) -> list[dict[str, Any]]:
        try:
            raw = self._registry_file.read_text()
            data = json.loads(raw) if raw.strip() else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write(self, entries: list[dict[str, Any]]) -> None:
        try:
            self._registry_file.write_text(json.dumps(entries, indent=2))
        except Exception as exc:
            logger.warning("Failed to write plugin registry: %s", exc)

    def _save_entries(self, entries: list[PluginEntry]) -> None:
        self._write([e.to_dict() for e in entries])

    def _load_entries(self) -> list[PluginEntry]:
        return [PluginEntry.from_dict(d) for d in self._read()]

    # -- public interface --------------------------------------------------

    def list_plugins(self) -> list[PluginEntry]:
        """List all registered plugins."""
        return self._load_entries()

    def get_plugin(self, plugin_id: str) -> PluginEntry | None:
        """Get a specific plugin entry by ID."""
        for entry in self._load_entries():
            if entry.id == plugin_id:
                return entry
        return None

    def install(
        self,
        spec: PluginSpec,
        source: str = "",
        *,
        auto_enable: bool = False,
    ) -> PluginEntry:
        """Register a plugin from a validated ``PluginSpec``.

        If the plugin is already installed, its entry is updated.
        """
        entries = self._load_entries()
        entry = PluginEntry.from_spec(spec, source=source)
        if auto_enable:
            entry.enabled = True
            entry.state = "enabled"

        # Replace existing or append
        found = False
        for i, existing in enumerate(entries):
            if existing.id == spec.id:
                entry.installed_at = existing.installed_at  # preserve original
                entries[i] = entry
                found = True
                break
        if not found:
            entries.append(entry)

        self._save_entries(entries)
        logger.info("Plugin %s %s registered (source=%s)", spec.id, spec.version, source)
        return entry

    def install_from_source(self, source: str) -> dict[str, Any]:
        """Install a plugin from a local path, git URL, or pip package.

        This is a convenience wrapper for CLI use. For manifest-based install,
        use ``install(spec)`` directly.

        Returns a dict with keys: ok (bool), message (str), entry (optional).
        """
        import os

        src = (source or "").strip()
        if not src:
            return {"ok": False, "message": "No source provided"}

        # Local path with plugin.yaml
        if os.path.exists(src):
            p = Path(src).resolve()
            manifest_path = p / "plugin.yaml" if p.is_dir() else p
            if p.is_dir() and not manifest_path.exists():
                # Try plugin.json
                manifest_path = p / "plugin.json"
            if manifest_path.exists():
                try:
                    from obscura.plugins.manifest import parse_manifest_file
                    spec = parse_manifest_file(manifest_path)
                    # Copy plugin to plugins dir
                    dest = self._plugin_dir / spec.id
                    if dest.exists():
                        shutil.rmtree(dest)
                    if p.is_dir():
                        shutil.copytree(p, dest)
                    else:
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, dest / p.name)
                    entry = self.install(spec, source=str(p))
                    return {"ok": True, "message": f"Installed {spec.id} v{spec.version}", "entry": entry}
                except Exception as exc:
                    return {"ok": False, "message": f"Manifest install failed: {exc}"}
            else:
                # Legacy: copy as local plugin (no manifest)
                try:
                    name = p.name
                    dest = self._plugin_dir / name
                    if dest.exists():
                        return {"ok": False, "message": f"Already exists: {dest}"}
                    if p.is_dir():
                        shutil.copytree(p, dest)
                    else:
                        shutil.copy2(p, dest)
                    return {"ok": True, "message": f"Copied local plugin to {dest}"}
                except Exception as exc:
                    return {"ok": False, "message": f"Local install failed: {exc}"}

        # Git URL
        if src.startswith("git+") or src.endswith(".git") or "github.com" in src:
            clone_url = src[4:] if src.startswith("git+") else src
            name = Path(clone_url.rstrip("/ ")).stem
            dest = self._plugin_dir / name
            if dest.exists():
                return {"ok": False, "message": f"Already exists: {dest}"}
            try:
                proc = subprocess.run(
                    ["git", "clone", clone_url, str(dest)],
                    capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    return {"ok": False, "message": f"git clone failed: {proc.stderr.strip()}"}
                # Try to parse manifest
                manifest_path = dest / "plugin.yaml"
                if manifest_path.exists():
                    from obscura.plugins.manifest import parse_manifest_file
                    spec = parse_manifest_file(manifest_path)
                    entry = self.install(spec, source=clone_url)
                    return {"ok": True, "message": f"Installed {spec.id} from git", "entry": entry}
                return {"ok": True, "message": f"Cloned to {dest} (no manifest found)"}
            except Exception as exc:
                return {"ok": False, "message": f"Git install failed: {exc}"}

        # Pip package
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", src],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                return {"ok": False, "message": f"pip install failed: {proc.stderr.strip()}"}
            # Record in registry (no manifest — entry-point discovery will find it)
            entries = self._load_entries()
            now = datetime.now(timezone.utc).isoformat()
            entry = PluginEntry(
                id=src.replace("[", "-").replace("]", "").replace(">=", "-"),
                name=src,
                version="0.0.0",
                source_type="pip",
                runtime_type="native",
                trust_level="community",
                author="",
                description=f"Installed via pip: {src}",
                source=src,
                enabled=True,
                state="installed",
                installed_at=now,
                updated_at=now,
            )
            entries.append(entry)
            self._save_entries(entries)
            return {"ok": True, "message": f"Installed pip package: {src}", "entry": entry}
        except Exception as exc:
            return {"ok": False, "message": f"pip install failed: {exc}"}

    def uninstall(self, plugin_id: str) -> bool:
        """Remove a plugin from the registry and clean up local files."""
        entries = self._load_entries()
        target: PluginEntry | None = None
        remaining: list[PluginEntry] = []
        for e in entries:
            if e.id == plugin_id:
                target = e
            else:
                remaining.append(e)

        if target is None:
            logger.warning("Plugin %s not found in registry", plugin_id)
            return False

        # Remove local files
        local_dir = self._plugin_dir / plugin_id
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)

        # Pip uninstall if applicable
        if target.source_type == "pip" and target.source:
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "uninstall", "-y", target.source],
                    capture_output=True, text=True,
                )
            except Exception as exc:
                logger.warning("pip uninstall failed for %s: %s", plugin_id, exc)

        self._save_entries(remaining)
        logger.info("Plugin %s uninstalled", plugin_id)
        return True

    def enable(self, plugin_id: str) -> bool:
        """Enable an installed plugin."""
        return self._set_enabled(plugin_id, True)

    def disable(self, plugin_id: str) -> bool:
        """Disable a plugin without removing it."""
        return self._set_enabled(plugin_id, False)

    def _set_enabled(self, plugin_id: str, enabled: bool) -> bool:
        entries = self._load_entries()
        for entry in entries:
            if entry.id == plugin_id:
                entry.enabled = enabled
                entry.state = "enabled" if enabled else "disabled"
                entry.updated_at = datetime.now(timezone.utc).isoformat()
                self._save_entries(entries)
                return True
        return False

    def get_status(self, plugin_id: str) -> PluginStatus | None:
        """Return lifecycle status for a plugin."""
        entry = self.get_plugin(plugin_id)
        if entry is None:
            return None
        return PluginStatus(
            plugin_id=entry.id,
            state=entry.state,
            error=entry.error,
            installed_at=entry.installed_at,
            updated_at=entry.updated_at,
            enabled=entry.enabled,
        )

    def get_contributions(self, plugin_id: str) -> dict[str, list[str]]:
        """Return the resource names contributed by a plugin."""
        entry = self.get_plugin(plugin_id)
        if entry is None:
            return {}
        return {
            "capabilities": entry.contributed_capabilities,
            "tools": entry.contributed_tools,
            "workflows": entry.contributed_workflows,
        }

    def list_enabled(self) -> list[PluginEntry]:
        """Return only enabled plugins."""
        return [e for e in self._load_entries() if e.enabled]

    @property
    def plugin_dir(self) -> Path:
        return self._plugin_dir


__all__ = ["PluginRegistryService", "PluginEntry"]
