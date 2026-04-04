"""obscura.plugins.claude_compat.marketplace — Git-based marketplace resolution.

Discovers, fetches, and caches Claude Code plugin marketplaces from
Git repositories.  A marketplace is a Git repo containing
``.claude-plugin/marketplace.json`` with a list of available plugins.

Usage::

    resolver = MarketplaceResolver()
    resolver.add_marketplace("my-org", source={"source": "github", "repo": "my-org/plugins"})
    plugins = resolver.list_plugins("my-org")
    resolver.install_plugin("my-plugin", "my-org")
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MARKETPLACES_DIR = Path.home() / ".obscura" / "plugins" / "claude_marketplaces"
_CACHE_DIR = Path.home() / ".obscura" / "plugins" / "claude_cache"
_KNOWN_MARKETPLACES_FILE = _MARKETPLACES_DIR / "known_marketplaces.json"

# Claude Code's official marketplace (if accessible).
_OFFICIAL_MARKETPLACE = "anthropics-claude-code"


@dataclass(frozen=True)
class MarketplaceSource:
    """How to fetch a marketplace."""

    source: str  # "github", "git", "url", "file", "directory"
    repo: str = ""  # for github: "owner/repo"
    url: str = ""  # for git/url
    path: str = ""  # for file/directory
    ref: str = ""  # git ref (branch/tag)


@dataclass
class MarketplaceEntry:
    """A single plugin entry from a marketplace manifest."""

    name: str
    description: str = ""
    version: str = ""
    source: str = ""  # how to fetch this plugin
    category: str = ""
    tags: list[str] = field(default_factory=list)
    author: str = ""


class MarketplaceResolver:
    """Resolve and cache Claude Code plugin marketplaces."""

    def __init__(self) -> None:
        _MARKETPLACES_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._known = self._load_known()

    # -- Marketplace management ------------------------------------------------

    def add_marketplace(
        self, name: str, source: dict[str, str] | MarketplaceSource
    ) -> bool:
        """Register a marketplace source.

        Parameters
        ----------
        name:
            Human-friendly marketplace name.
        source:
            Source spec (dict or MarketplaceSource).

        Returns True if the marketplace was fetched successfully.
        """
        if isinstance(source, dict):
            source = MarketplaceSource(**source)

        self._known[name] = source
        self._save_known()

        # Fetch immediately.
        return self.fetch_marketplace(name)

    def remove_marketplace(self, name: str) -> bool:
        """Remove a registered marketplace and its cached data."""
        if name not in self._known:
            return False
        del self._known[name]
        self._save_known()

        # Clean up cached marketplace.
        market_dir = _MARKETPLACES_DIR / name
        if market_dir.exists():
            shutil.rmtree(market_dir, ignore_errors=True)
        return True

    def list_marketplaces(self) -> dict[str, MarketplaceSource]:
        """Return all registered marketplaces."""
        return dict(self._known)

    def fetch_marketplace(self, name: str) -> bool:
        """Fetch/update a marketplace from its source.

        Returns True on success.
        """
        source = self._known.get(name)
        if source is None:
            logger.warning("Unknown marketplace: %s", name)
            return False

        target = _MARKETPLACES_DIR / name

        try:
            if source.source == "github":
                return self._fetch_github(source.repo, target, ref=source.ref)
            if source.source == "git":
                return self._fetch_git(source.url, target, ref=source.ref)
            if source.source == "directory":
                # Symlink to local directory.
                if target.exists():
                    target.unlink() if target.is_symlink() else shutil.rmtree(target)
                target.symlink_to(Path(source.path).expanduser().resolve())
                return True
            if source.source in ("url", "file"):
                return self._fetch_url_or_file(source, target)
            logger.warning("Unsupported marketplace source type: %s", source.source)
            return False
        except Exception:
            logger.warning("Failed to fetch marketplace %s", name, exc_info=True)
            return False

    # -- Plugin discovery ------------------------------------------------------

    def list_plugins(self, marketplace: str) -> list[MarketplaceEntry]:
        """List all plugins available in a marketplace.

        Supports both Obscura-native ``marketplace.toml`` and Claude Code
        ``marketplace.json`` formats.
        """
        market_dir = _MARKETPLACES_DIR / marketplace

        # Try Obscura-native marketplace.toml first.
        toml_path = market_dir / "marketplace.toml"
        if toml_path.exists():
            return self._parse_obscura_marketplace(toml_path)

        # Fall back to Claude Code marketplace.json.
        manifest = self._find_marketplace_manifest(market_dir)
        if manifest is None:
            logger.debug("No marketplace manifest for %s", marketplace)
            return []

        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            logger.debug(
                "Could not parse marketplace manifest %s", manifest, exc_info=True
            )
            return []

        plugins_raw = data.get("plugins", [])
        if isinstance(plugins_raw, dict):
            plugins_raw = list(plugins_raw.values())

        entries: list[MarketplaceEntry] = []
        for p in plugins_raw:
            if not isinstance(p, dict):
                continue
            entries.append(
                MarketplaceEntry(
                    name=p.get("name", ""),
                    description=p.get("description", ""),
                    version=p.get("version", ""),
                    source=p.get("source", ""),
                    category=p.get("category", ""),
                    tags=p.get("tags", []),
                    author=_extract_author_str(p.get("author")),
                )
            )
        return entries

    def _parse_obscura_marketplace(self, toml_path: Path) -> list[MarketplaceEntry]:
        """Parse an Obscura-native ``marketplace.toml``."""
        try:
            from obscura.plugins.claude_compat.obscura_marketplace import (
                parse_marketplace_toml,
            )

            marketplace = parse_marketplace_toml(toml_path)
            if marketplace is None:
                return []
            return [
                MarketplaceEntry(
                    name=p.name,
                    description=p.description,
                    version=p.version,
                    source=p.source or p.path,
                    category=p.format,  # "obscura" or "claude"
                    tags=p.tags,
                    author=p.author,
                )
                for p in marketplace.plugins
            ]
        except Exception:
            logger.debug(
                "Could not parse Obscura marketplace %s", toml_path, exc_info=True
            )
            return []

    def install_plugin(
        self,
        plugin_name: str,
        marketplace: str,
    ) -> Path | None:
        """Install a plugin from a marketplace into the cache.

        Handles both Obscura-native and Claude Code plugin formats.
        Returns the plugin install path on success, None on failure.
        """
        entries = self.list_plugins(marketplace)
        entry = next((e for e in entries if e.name == plugin_name), None)
        if entry is None:
            logger.warning(
                "Plugin %s not found in marketplace %s", plugin_name, marketplace
            )
            return None

        market_dir = _MARKETPLACES_DIR / marketplace

        # Check if plugin is bundled inside the marketplace repo (by path).
        source_or_path = entry.source or ""
        plugin_path = (
            market_dir / source_or_path if source_or_path else market_dir / plugin_name
        )

        if plugin_path.is_dir():
            # Detect format: Claude Code (.claude-plugin/plugin.json) or Obscura (plugin.toml).
            is_bundled = (plugin_path / ".claude-plugin" / "plugin.json").exists() or (
                plugin_path / "plugin.toml"
            ).exists()
            if is_bundled:
                cache_path = (
                    _CACHE_DIR / marketplace / plugin_name / (entry.version or "latest")
                )
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                if cache_path.exists():
                    shutil.rmtree(cache_path)
                shutil.copytree(plugin_path, cache_path)
                logger.info(
                    "Installed %s@%s to %s", plugin_name, marketplace, cache_path
                )
                return cache_path

        # Plugin has a remote source — clone/download it.
        if source_or_path and source_or_path.startswith(
            ("https://", "git@", "ssh://", "/")
        ):
            cache_path = (
                _CACHE_DIR / marketplace / plugin_name / (entry.version or "latest")
            )
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            if self._fetch_plugin_source(source_or_path, cache_path):
                logger.info(
                    "Installed %s@%s to %s", plugin_name, marketplace, cache_path
                )
                return cache_path

        logger.warning("Could not install %s from %s", plugin_name, marketplace)
        return None

    # -- Internal: Git operations ----------------------------------------------

    def _fetch_github(self, repo: str, target: Path, *, ref: str = "") -> bool:
        """Clone or pull a GitHub repo."""
        url = f"https://github.com/{repo}.git"
        return self._fetch_git(url, target, ref=ref)

    def _fetch_git(self, url: str, target: Path, *, ref: str = "") -> bool:
        """Clone or pull a git repo to *target*."""
        if target.exists() and (target / ".git").exists():
            # Pull.
            result = subprocess.run(
                ["git", "-C", str(target), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return result.returncode == 0

        # Clone.
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd.extend(["--branch", ref])
        cmd.extend([url, str(target)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.warning("git clone failed: %s", result.stderr[:200])
        return result.returncode == 0

    def _fetch_url_or_file(self, source: MarketplaceSource, target: Path) -> bool:
        """Download a URL or copy a file to *target*."""
        src = source.url or source.path
        if not src:
            return False

        target.mkdir(parents=True, exist_ok=True)
        dest = target / "marketplace.json"

        if src.startswith(("http://", "https://")):
            try:
                import urllib.request

                urllib.request.urlretrieve(src, str(dest))
                return True
            except Exception:
                logger.debug("URL fetch failed: %s", src, exc_info=True)
                return False

        # Local file.
        src_path = Path(src).expanduser()
        if src_path.exists():
            shutil.copy2(src_path, dest)
            return True
        return False

    def _fetch_plugin_source(self, source: str, target: Path) -> bool:
        """Fetch a single plugin from its source string.

        Supports git URLs and local paths.
        """
        if source.startswith(("https://", "git@", "ssh://")):
            return self._fetch_git(source, target)
        src_path = Path(source).expanduser()
        if src_path.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src_path, target)
            return True
        return False

    # -- Internal: Manifest discovery ------------------------------------------

    def _find_marketplace_manifest(self, market_dir: Path) -> Path | None:
        """Find marketplace.json in a marketplace directory."""
        # Standard: .claude-plugin/marketplace.json
        std = market_dir / ".claude-plugin" / "marketplace.json"
        if std.exists():
            return std
        # Flat: marketplace.json at root.
        flat = market_dir / "marketplace.json"
        if flat.exists():
            return flat
        return None

    # -- Persistence -----------------------------------------------------------

    def _load_known(self) -> dict[str, MarketplaceSource]:
        """Load known marketplaces from disk."""
        if not _KNOWN_MARKETPLACES_FILE.exists():
            return {}
        try:
            data = json.loads(_KNOWN_MARKETPLACES_FILE.read_text(encoding="utf-8"))
            result: dict[str, MarketplaceSource] = {}
            for name, spec in data.get("marketplaces", {}).items():
                if isinstance(spec, dict):
                    result[name] = MarketplaceSource(
                        **{
                            k: v
                            for k, v in spec.items()
                            if k in MarketplaceSource.__dataclass_fields__
                        }
                    )
            return result
        except Exception:
            return {}

    def _save_known(self) -> None:
        """Persist known marketplaces to disk."""
        _MARKETPLACES_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1",
            "marketplaces": {
                name: {
                    "source": src.source,
                    "repo": src.repo,
                    "url": src.url,
                    "path": src.path,
                    "ref": src.ref,
                }
                for name, src in self._known.items()
            },
        }
        _KNOWN_MARKETPLACES_FILE.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )


def _extract_author_str(author: Any) -> str:
    if isinstance(author, dict):
        return author.get("name", "")
    if isinstance(author, str):
        return author
    return ""
