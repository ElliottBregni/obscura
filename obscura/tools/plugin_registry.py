"""Simple plugin registry and installer for Obscura local/remote plugins.

This module provides a lightweight registry stored under ~/.obscura/plugins/registry.json
and helper functions to install local/git/pip plugins into the Obscura plugins directory.

Note: This is intentionally minimal. Pip installs are delegated to the system Python's
pip subprocess, and git clones require `git` to be available on PATH. Local installs copy
source files into the plugins directory. Registry entries are a JSON array of objects with
minimal metadata.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from obscura.core.paths import resolve_obscura_home


class PluginRegistry:
    """Manage installed plugins in ~/.obscura/plugins.

    Registry file: ~/.obscura/plugins/registry.json (array of objects)
    """

    def __init__(self, plugin_dir: Optional[Path] = None) -> None:
        self.home = resolve_obscura_home()
        self.plugin_dir = Path(plugin_dir) if plugin_dir is not None else (self.home / "plugins")
        # Ensure plugin directory exists
        try:
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Best-effort; some callers may create dirs externally
            pass

        self.registry_file = self.plugin_dir / "registry.json"
        if not self.registry_file.exists():
            self._write_registry([])

    # -- registry helpers -------------------------------------------------
    def _read_registry(self) -> List[Dict[str, Any]]:
        try:
            raw = self.registry_file.read_text()
            data = json.loads(raw) if raw.strip() else []
            if isinstance(data, list):
                return data
            return []
        except Exception:
            return []

    def _write_registry(self, data: List[Dict[str, Any]]) -> None:
        try:
            self.registry_file.write_text(json.dumps(data, indent=2))
        except Exception:
            # If writing fails, ignore — runtime will still work with local plugins
            pass

    # -- listing ----------------------------------------------------------
    def list_installed(self) -> Dict[str, Any]:
        """Return a dict with local plugins and registered (pip/git) entries."""
        regs = self._read_registry()
        local: List[Dict[str, Any]] = []
        try:
            for entry in sorted(self.plugin_dir.iterdir()):
                if entry.name == self.registry_file.name:
                    continue
                if entry.is_dir():
                    if (entry / "__init__.py").exists():
                        local.append({"name": entry.name, "type": "local", "path": str(entry)})
                elif entry.suffix == ".py":
                    local.append({"name": entry.stem, "type": "local", "path": str(entry)})
        except Exception:
            pass

        return {"local": local, "registered": regs}

    # -- install ----------------------------------------------------------
    def install(self, source: str) -> Dict[str, Any]:
        """Install a plugin from a local path, git URL, or pip package.

        Returns a dict with keys: ok (bool), message (str), name (optional)
        """
        src = (source or "").strip()
        if not src:
            return {"ok": False, "message": "No source provided"}

        # Local path
        if os.path.exists(src):
            try:
                p = Path(src).resolve()
                name = p.name
                dest = self.plugin_dir / name
                if dest.exists():
                    return {"ok": False, "message": f"Destination already exists: {dest}"}
                if p.is_dir():
                    shutil.copytree(p, dest)
                else:
                    # single file
                    dest_file = dest.with_suffix(p.suffix or ".py")
                    shutil.copy2(p, dest_file)
                return {"ok": True, "message": f"Installed local plugin to {dest}", "name": name}
            except Exception as exc:
                return {"ok": False, "message": f"Local install failed: {exc}"}

        # Git URL
        if src.startswith("git+") or src.endswith(".git") or "github.com" in src:
            clone_url = src[4:] if src.startswith("git+") else src
            try:
                name = Path(clone_url.rstrip("/ ")).stem
                dest = self.plugin_dir / name
                if dest.exists():
                    return {"ok": False, "message": f"Destination already exists: {dest}"}
                proc = subprocess.run(["git", "clone", clone_url, str(dest)], capture_output=True, text=True)
                if proc.returncode != 0:
                    return {"ok": False, "message": f"git clone failed: {proc.stderr.strip()}"}
                regs = self._read_registry()
                regs.append({"name": name, "type": "git", "source": clone_url, "path": str(dest)})
                self._write_registry(regs)
                return {"ok": True, "message": f"Cloned plugin to {dest}", "name": name}
            except Exception as exc:
                return {"ok": False, "message": f"Git install failed: {exc}"}

        # Fallback: pip install
        try:
            proc = subprocess.run([sys.executable, "-m", "pip", "install", src], capture_output=True, text=True)
            if proc.returncode != 0:
                return {"ok": False, "message": f"pip install failed: {proc.stderr.strip()}"}
            regs = self._read_registry()
            regs.append({"name": src, "type": "pip", "package": src})
            self._write_registry(regs)
            return {"ok": True, "message": f"Installed pip package {src}", "name": src}
        except Exception as exc:
            return {"ok": False, "message": f"pip install failed: {exc}"}

    # -- remove -----------------------------------------------------------
    def remove(self, name: str) -> Dict[str, Any]:
        """Uninstall or remove a plugin by name.

        Attempts local removal first, then looks for registry entries (pip/git).
        """
        if not name:
            return {"ok": False, "message": "No name provided"}

        # Local plugin removal
        try:
            p = self.plugin_dir / name
            if p.exists():
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)  # type: ignore[attr-defined]
                # Remove any registry entries pointing here
                regs = self._read_registry()
                regs = [r for r in regs if r.get("path") != str(p) and r.get("name") != name]
                self._write_registry(regs)
                return {"ok": True, "message": f"Removed local plugin {name}"}
        except Exception as exc:
            return {"ok": False, "message": f"Failed to remove local plugin: {exc}"}

        # Registry-based uninstall (pip/git)
        regs = self._read_registry()
        for r in list(regs):
            if r.get("name") == name or r.get("package") == name:
                try:
                    if r.get("type") == "pip":
                        proc = subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", r.get("package")], capture_output=True, text=True)
                        if proc.returncode != 0:
                            return {"ok": False, "message": f"pip uninstall failed: {proc.stderr.strip()}"}
                    elif r.get("type") == "git":
                        path = Path(r.get("path", ""))
                        if path.exists():
                            shutil.rmtree(path)
                    regs.remove(r)
                    self._write_registry(regs)
                    return {"ok": True, "message": f"Uninstalled plugin {name}"}
                except Exception as exc:
                    return {"ok": False, "message": f"Failed to uninstall plugin: {exc}"}

        return {"ok": False, "message": "Plugin not found"}


__all__ = ["PluginRegistry"]
