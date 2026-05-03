"""obscura.plugins.claude_compat.obscura_marketplace — Obscura-native marketplace support.

An Obscura marketplace is a Git repo (or local directory) with a
``marketplace.toml`` manifest listing available plugins.  It supports
both native Obscura plugins (TOML-based) and Claude Code plugins
(plugin.json-based) in the same registry.

Creating a marketplace
----------------------

1. Create a Git repo with this structure::

    my-marketplace/
    ├── marketplace.toml              # Index of available plugins
    ├── plugins/
    │   ├── my-native-plugin/
    │   │   └── plugin.toml           # Obscura native plugin
    │   ├── my-claude-plugin/
    │   │   └── .claude-plugin/
    │   │       └── plugin.json       # Claude Code plugin
    │   └── another-plugin/
    │       └── plugin.toml
    └── README.md

2. Write ``marketplace.toml``::

    [marketplace]
    name = "my-marketplace"
    description = "My plugins for Obscura"
    author = "Your Name"
    url = "https://github.com/you/my-marketplace"

    [[plugins]]
    id = "my-native-plugin"
    name = "My Native Plugin"
    description = "Does cool things"
    version = "1.0.0"
    format = "obscura"               # or "claude" for Claude Code plugins
    path = "plugins/my-native-plugin" # relative to repo root
    tags = ["productivity", "git"]

    [[plugins]]
    id = "my-claude-plugin"
    name = "My Claude Plugin"
    description = "A Claude Code plugin that works in Obscura"
    version = "0.2.0"
    format = "claude"
    path = "plugins/my-claude-plugin"
    tags = ["ai", "code-review"]
    # Or use a remote source:
    # source = "https://github.com/someone/plugin.git"

3. Push to GitHub and register::

    /plugin marketplace add you/my-marketplace

Usage::

    from obscura.plugins.claude_compat.obscura_marketplace import (
        ObscuraMarketplace,
        parse_marketplace_toml,
        scaffold_marketplace,
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


def _empty_str_list() -> list[str]:
    return []


def _empty_plugin_list() -> list[ObscuraMarketplacePlugin]:
    return []


@dataclass
class ObscuraMarketplacePlugin:
    """A plugin entry in an Obscura marketplace."""

    id: str
    name: str
    description: str = ""
    version: str = "0.0.0"
    format: str = "obscura"  # "obscura" | "claude"
    path: str = ""  # relative path in repo
    source: str = ""  # remote source (git URL) if not bundled
    tags: list[str] = field(default_factory=_empty_str_list)
    author: str = ""
    license: str = ""


@dataclass
class ObscuraMarketplace:
    """Parsed Obscura marketplace manifest."""

    name: str
    description: str = ""
    author: str = ""
    url: str = ""
    plugins: list[ObscuraMarketplacePlugin] = field(default_factory=_empty_plugin_list)


def _as_str(value: Any, default: str = "") -> str:
    """Coerce *value* to ``str`` (returning *default* when not a string)."""
    return value if isinstance(value, str) else default


def _as_str_list(value: Any) -> list[str]:
    """Coerce *value* to ``list[str]`` (filtering non-strings)."""
    if not isinstance(value, list):
        return []
    return [v for v in cast(list[Any], value) if isinstance(v, str)]


def parse_marketplace_toml(path: Path) -> ObscuraMarketplace | None:
    """Parse a ``marketplace.toml`` file.

    Returns None if the file can't be parsed.
    """
    if not path.exists():
        return None

    try:
        import tomllib as _tomllib
    except ImportError:
        try:
            import tomli as _tomllib  # type: ignore[no-redef]
        except ImportError:
            logger.warning("No TOML parser available (need Python 3.11+ or tomli)")
            return None

    tomllib_any: Any = _tomllib
    try:
        data: dict[str, Any] = cast(
            dict[str, Any], tomllib_any.loads(path.read_text(encoding="utf-8"))
        )
    except Exception:
        logger.warning("Could not parse %s", path, exc_info=True)
        return None

    meta_raw = data.get("marketplace", {})
    meta: dict[str, Any] = (
        cast(dict[str, Any], meta_raw)
        if isinstance(meta_raw, dict)
        else cast(dict[str, Any], {})
    )
    plugins_raw_obj = data.get("plugins", [])
    plugins_raw: list[Any] = (
        cast(list[Any], plugins_raw_obj) if isinstance(plugins_raw_obj, list) else []
    )

    plugins: list[ObscuraMarketplacePlugin] = []
    for p_obj in plugins_raw:
        if not isinstance(p_obj, dict):
            continue
        p: dict[str, Any] = cast(dict[str, Any], p_obj)
        plugins.append(
            ObscuraMarketplacePlugin(
                id=_as_str(p.get("id", p.get("name", ""))),
                name=_as_str(p.get("name", p.get("id", ""))),
                description=_as_str(p.get("description", "")),
                version=_as_str(p.get("version", "0.0.0"), "0.0.0"),
                format=_as_str(p.get("format", "obscura"), "obscura"),
                path=_as_str(p.get("path", "")),
                source=_as_str(p.get("source", "")),
                tags=_as_str_list(p.get("tags", [])),
                author=_as_str(p.get("author", "")),
                license=_as_str(p.get("license", "")),
            )
        )

    return ObscuraMarketplace(
        name=_as_str(meta.get("name", path.parent.name), path.parent.name),
        description=_as_str(meta.get("description", "")),
        author=_as_str(meta.get("author", "")),
        url=_as_str(meta.get("url", "")),
        plugins=plugins,
    )


def scaffold_marketplace(
    target_dir: Path,
    name: str = "my-marketplace",
    *,
    author: str = "",
) -> Path:
    """Create a starter marketplace directory with example structure.

    Returns the path to the created ``marketplace.toml``.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    plugins_dir = target_dir / "plugins"
    plugins_dir.mkdir(exist_ok=True)

    # Example native plugin.
    example_native = plugins_dir / "example-native"
    example_native.mkdir(exist_ok=True)
    (example_native / "plugin.toml").write_text(
        'id = "example-native"\n'
        'name = "Example Native Plugin"\n'
        'version = "0.1.0"\n'
        'source_type = "local"\n'
        'runtime_type = "native"\n'
        'description = "A starter Obscura plugin"\n\n'
        "[[capabilities]]\n"
        'id = "example.core"\n'
        'description = "Example capability"\n'
        'tools = ["example_tool"]\n\n'
        "[[tools]]\n"
        'name = "example_tool"\n'
        'description = "An example tool"\n'
        'handler = "example_native.tools:run"\n'
        'capability = "example.core"\n',
        encoding="utf-8",
    )

    # Example Claude Code plugin.
    example_claude = plugins_dir / "example-claude"
    example_claude.mkdir(exist_ok=True)
    claude_dir = example_claude / ".claude-plugin"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / "plugin.json").write_text(
        "{\n"
        '  "name": "example-claude",\n'
        '  "version": "0.1.0",\n'
        '  "description": "A starter Claude Code plugin for Obscura"\n'
        "}\n",
        encoding="utf-8",
    )
    skills_dir = example_claude / "skills" / "hello"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(
        "---\n"
        "description: Say hello\n"
        "---\n\n"
        "Say hello to the user in a friendly way.\n",
        encoding="utf-8",
    )

    # Marketplace manifest.
    author_line = f'author = "{author}"\n' if author else ""
    manifest_path = target_dir / "marketplace.toml"
    manifest_path.write_text(
        f"[marketplace]\n"
        f'name = "{name}"\n'
        f'description = "Plugins for Obscura"\n'
        f"{author_line}\n"
        f"[[plugins]]\n"
        f'id = "example-native"\n'
        f'name = "Example Native Plugin"\n'
        f'description = "A starter Obscura plugin"\n'
        f'version = "0.1.0"\n'
        f'format = "obscura"\n'
        f'path = "plugins/example-native"\n'
        f'tags = ["example"]\n\n'
        f"[[plugins]]\n"
        f'id = "example-claude"\n'
        f'name = "Example Claude Plugin"\n'
        f'description = "A starter Claude Code plugin"\n'
        f'version = "0.1.0"\n'
        f'format = "claude"\n'
        f'path = "plugins/example-claude"\n'
        f'tags = ["example"]\n',
        encoding="utf-8",
    )

    # README.
    (target_dir / "README.md").write_text(
        f"# {name}\n\n"
        f"An Obscura plugin marketplace.\n\n"
        f"## Plugins\n\n"
        f"| Plugin | Format | Description |\n"
        f"|--------|--------|-------------|\n"
        f"| example-native | obscura | A starter Obscura plugin |\n"
        f"| example-claude | claude | A starter Claude Code plugin |\n\n"
        f"## Usage\n\n"
        f"```\n"
        f"/plugin marketplace add <github-user>/{name}\n"
        f"/plugin install example-native@{name}\n"
        f"```\n",
        encoding="utf-8",
    )

    logger.info("Scaffolded marketplace at %s", target_dir)
    return manifest_path
