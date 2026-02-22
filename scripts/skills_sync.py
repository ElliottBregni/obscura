#!/usr/bin/env python3
"""Agent skills & tools sync — discover, copy, index, and redistribute.

Finds all skills, commands, plugins, automations, and configs from
~/.claude, ~/.copilot, and ~/.codex, copies them into
~/dev/.obscura/agents/skills/, produces a unified index (INDEX.jsonl),
and feeds portable items into ~/dev/vault/ for redistribution.

Complements agent_sync.py (session sync) and sync.py (vault config sync).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILLS_DIR = Path.home() / "dev" / ".obscura" / "agents" / "skills"
INDEX_FILE = SKILLS_DIR / "INDEX.jsonl"
VAULT_DIR = Path.home() / "dev" / "vault"


@dataclass
class SkillSource:
    """Configuration for one agent's skill source."""

    name: str
    source_dir: Path


SKILL_SOURCES: dict[str, SkillSource] = {
    "claude": SkillSource(name="claude", source_dir=Path.home() / ".claude"),
    "copilot": SkillSource(name="copilot", source_dir=Path.home() / ".copilot"),
    "codex": SkillSource(name="codex", source_dir=Path.home() / ".codex"),
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredItem:
    """A discovered skill, command, plugin, config, automation, or rule."""

    agent: str
    item_type: str  # skill, command, plugin, config, automation, rule
    name: str
    source_path: Path
    files: list[tuple[Path, Path]]  # (source_abs, dest_relative)
    mtime: float = 0.0
    frontmatter: dict[str, str] = field(default_factory=lambda: dict[str, str]())


@dataclass
class SkillEntry:
    """One entry in INDEX.jsonl."""

    id: str  # "claude:skill:red-team"
    agent: str
    type: str
    name: str
    description: str
    source_path: str
    synced_path: str
    files: list[str]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "source_path": self.source_path,
            "synced_path": self.synced_path,
            "files": self.files,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Parse YAML frontmatter between --- delimiters.

    Handles simple key: value pairs (no nested structures).
    """
    if not path.is_file():
        return {}

    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                result[key] = value

    return result


def _parse_simple_toml(path: Path) -> dict[str, Any]:
    """Parse flat TOML files (key = value).

    Handles strings, numbers, booleans, and simple arrays.
    Ignores [section] headers but includes their keys.
    """
    if not path.is_file():
        return {}

    result: dict[str, Any] = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            key = key.strip().strip('"')
            value = value.strip()

            # String
            if value.startswith('"') and value.endswith('"'):
                result[key] = value[1:-1]
            # Boolean
            elif value in ("true", "false"):
                result[key] = value == "true"
            # Array (simple)
            elif value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                if not inner:
                    result[key] = []
                else:
                    items = [
                        i.strip().strip('"') for i in inner.split(",") if i.strip()
                    ]
                    result[key] = items
            # Number
            else:
                try:
                    result[key] = int(value)
                except ValueError:
                    try:
                        result[key] = float(value)
                    except ValueError:
                        result[key] = value
    except OSError:
        pass

    return result


def _safe_read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning empty dict on error."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}


def _collapse_home(p: Path) -> str:
    """Collapse home directory to ~ for display."""
    home = str(Path.home())
    s = str(p)
    if s.startswith(home):
        return "~" + s[len(home) :]
    return s


def _collect_dir_files(
    dir_path: Path, dest_prefix: Path | None = None
) -> list[tuple[Path, Path]]:
    """Collect all files in a directory with relative dest paths."""
    files: list[tuple[Path, Path]] = []
    if not dir_path.is_dir():
        return files
    for f in sorted(dir_path.rglob("*")):
        if f.is_file():
            rel = f.relative_to(dir_path)
            dest_rel = dest_prefix / rel if dest_prefix else rel
            files.append((f, dest_rel))
    return files


def _max_mtime(files: list[tuple[Path, Path]]) -> float:
    """Get the maximum mtime from a list of file tuples."""
    mtime = 0.0
    for src, _ in files:
        try:
            mtime = max(mtime, src.stat().st_mtime)
        except OSError:
            pass
    return mtime


# ---------------------------------------------------------------------------
# SkillDiscovery — find skills, commands, plugins, configs
# ---------------------------------------------------------------------------


class SkillDiscovery:
    """Discovers skills, commands, plugins, and configs from agent dirs."""

    def discover(self, source: SkillSource) -> list[DiscoveredItem]:
        if not source.source_dir.is_dir():
            return []
        dispatch = {
            "claude": self._discover_claude,
            "copilot": self._discover_copilot,
            "codex": self._discover_codex,
        }
        fn = dispatch.get(source.name)
        return fn(source) if fn else []

    # -- Claude -------------------------------------------------------------

    def _discover_claude(self, source: SkillSource) -> list[DiscoveredItem]:
        base = source.source_dir
        items: list[DiscoveredItem] = []

        # Skills: skills/*/SKILL.md
        skills_dir = base / "skills"
        if skills_dir.is_dir():
            for skill_dir in sorted(skills_dir.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                    continue
                files = _collect_dir_files(skill_dir)
                if not files:
                    continue
                fm = _parse_frontmatter(skill_dir / "SKILL.md")
                items.append(
                    DiscoveredItem(
                        agent="claude",
                        item_type="skill",
                        name=skill_dir.name,
                        source_path=skill_dir,
                        files=files,
                        mtime=_max_mtime(files),
                        frontmatter=fm,
                    )
                )

        # Commands: commands/*.md
        commands_dir = base / "commands"
        if commands_dir.is_dir():
            for cmd_file in sorted(commands_dir.iterdir()):
                if not cmd_file.is_file() or cmd_file.suffix != ".md":
                    continue
                fm = _parse_frontmatter(cmd_file)
                name = cmd_file.stem
                files = [(cmd_file, Path(cmd_file.name))]
                items.append(
                    DiscoveredItem(
                        agent="claude",
                        item_type="command",
                        name=name,
                        source_path=cmd_file,
                        files=files,
                        mtime=_max_mtime(files),
                        frontmatter=fm,
                    )
                )

        # Plugins: plugins/marketplaces/claude-plugins-official/plugins/*/
        plugins_base = (
            base / "plugins" / "marketplaces" / "claude-plugins-official" / "plugins"
        )
        if plugins_base.is_dir():
            for plugin_dir in sorted(plugins_base.iterdir()):
                if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                    continue
                files = _collect_dir_files(plugin_dir)
                if not files:
                    continue
                # Parse plugin.json — may be at root or in .claude-plugin/
                pjson_path = plugin_dir / "plugin.json"
                if not pjson_path.is_file():
                    pjson_path = plugin_dir / ".claude-plugin" / "plugin.json"
                pjson = _safe_read_json(pjson_path)
                fm: dict[str, str] = {}
                if pjson:
                    fm["name"] = str(pjson.get("name", plugin_dir.name))
                    fm["description"] = str(pjson.get("description", ""))
                    fm["version"] = str(pjson.get("version", ""))
                    author_val: Any = pjson.get("author", {})
                    if isinstance(author_val, dict):
                        author_dict: dict[str, Any] = author_val  # type: ignore[assignment]
                        fm["author"] = str(author_dict.get("name", ""))
                else:
                    fm["name"] = plugin_dir.name
                items.append(
                    DiscoveredItem(
                        agent="claude",
                        item_type="plugin",
                        name=plugin_dir.name,
                        source_path=plugin_dir,
                        files=files,
                        mtime=_max_mtime(files),
                        frontmatter=fm,
                    )
                )

        # Config files
        for config_name in ("settings.json", "settings.local.json"):
            config_path = base / config_name
            if config_path.is_file():
                try:
                    mtime = config_path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                items.append(
                    DiscoveredItem(
                        agent="claude",
                        item_type="config",
                        name=config_name,
                        source_path=config_path,
                        files=[(config_path, Path(config_name))],
                        mtime=mtime,
                    )
                )

        return items

    # -- Copilot ------------------------------------------------------------

    def _discover_copilot(self, source: SkillSource) -> list[DiscoveredItem]:
        base = source.source_dir
        items: list[DiscoveredItem] = []

        config_path = base / "config.json"
        if config_path.is_file():
            try:
                mtime = config_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            items.append(
                DiscoveredItem(
                    agent="copilot",
                    item_type="config",
                    name="config.json",
                    source_path=config_path,
                    files=[(config_path, Path("config.json"))],
                    mtime=mtime,
                )
            )

        return items

    # -- Codex --------------------------------------------------------------

    def _discover_codex(self, source: SkillSource) -> list[DiscoveredItem]:
        base = source.source_dir
        items: list[DiscoveredItem] = []

        # Skills: skills/*/ (including .system/*)
        skills_dir = base / "skills"
        if skills_dir.is_dir():
            # Custom skills
            for skill_dir in sorted(skills_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                if skill_dir.name == ".system":
                    # System skills
                    for sys_skill in sorted(skill_dir.iterdir()):
                        if not sys_skill.is_dir():
                            continue
                        files = _collect_dir_files(sys_skill)
                        if not files:
                            continue
                        fm = _parse_frontmatter(sys_skill / "SKILL.md")
                        fm["system"] = "true"
                        items.append(
                            DiscoveredItem(
                                agent="codex",
                                item_type="skill",
                                name=f".system/{sys_skill.name}",
                                source_path=sys_skill,
                                files=files,
                                mtime=_max_mtime(files),
                                frontmatter=fm,
                            )
                        )
                else:
                    files = _collect_dir_files(skill_dir)
                    if not files:
                        continue
                    fm = _parse_frontmatter(skill_dir / "SKILL.md")
                    items.append(
                        DiscoveredItem(
                            agent="codex",
                            item_type="skill",
                            name=skill_dir.name,
                            source_path=skill_dir,
                            files=files,
                            mtime=_max_mtime(files),
                            frontmatter=fm,
                        )
                    )

        # Automations: automations/*/automation.toml
        automations_dir = base / "automations"
        if automations_dir.is_dir():
            for auto_dir in sorted(automations_dir.iterdir()):
                if not auto_dir.is_dir():
                    continue
                toml_path = auto_dir / "automation.toml"
                if not toml_path.is_file():
                    continue
                toml_data = _parse_simple_toml(toml_path)
                fm: dict[str, str] = {
                    "name": str(toml_data.get("name", auto_dir.name)),
                    "status": str(toml_data.get("status", "")),
                }
                if toml_data.get("rrule"):
                    fm["schedule"] = str(toml_data["rrule"])
                if toml_data.get("prompt"):
                    prompt = str(toml_data["prompt"])
                    fm["prompt"] = prompt[:120] + "..." if len(prompt) > 120 else prompt
                files = _collect_dir_files(auto_dir)
                items.append(
                    DiscoveredItem(
                        agent="codex",
                        item_type="automation",
                        name=auto_dir.name,
                        source_path=auto_dir,
                        files=files,
                        mtime=_max_mtime(files),
                        frontmatter=fm,
                    )
                )

        # Config: config.toml
        config_path = base / "config.toml"
        if config_path.is_file():
            try:
                mtime = config_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            items.append(
                DiscoveredItem(
                    agent="codex",
                    item_type="config",
                    name="config.toml",
                    source_path=config_path,
                    files=[(config_path, Path("config.toml"))],
                    mtime=mtime,
                )
            )

        # Rules: rules/
        rules_dir = base / "rules"
        if rules_dir.is_dir():
            files = _collect_dir_files(rules_dir)
            if files:
                items.append(
                    DiscoveredItem(
                        agent="codex",
                        item_type="rule",
                        name="rules",
                        source_path=rules_dir,
                        files=files,
                        mtime=_max_mtime(files),
                    )
                )

        return items


# ---------------------------------------------------------------------------
# SkillCopier — incremental file sync to .obscura
# ---------------------------------------------------------------------------


class SkillCopier:
    """Copy skill files to the sync destination with mtime-based skipping."""

    def __init__(self, dest_base: Path, dry_run: bool = False) -> None:
        self.dest_base = dest_base
        self.dry_run = dry_run

    def sync_item(
        self,
        item: DiscoveredItem,
        force: bool = False,
    ) -> tuple[int, int]:
        """Copy item files. Returns (copied, skipped)."""
        # Dest: {base}/{agent}/{type}s/{name}/ (e.g., claude/skills/red-team/)
        # Commands and configs are flat files, not directories
        if item.item_type == "config":
            dest_dir = self.dest_base / item.agent / "config"
        elif item.item_type == "command":
            dest_dir = self.dest_base / item.agent / "commands"
        elif item.item_type == "rule":
            dest_dir = self.dest_base / item.agent / "rules"
        else:
            dest_dir = self.dest_base / item.agent / f"{item.item_type}s" / item.name

        copied = 0
        skipped = 0

        for source_abs, dest_rel in item.files:
            dest_file = dest_dir / dest_rel

            if not force and dest_file.is_file():
                try:
                    if dest_file.stat().st_mtime >= source_abs.stat().st_mtime:
                        skipped += 1
                        continue
                except OSError:
                    pass

            if not self.dry_run:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_abs, dest_file)
            copied += 1

        return copied, skipped

    def get_last_sync(self, agent: str) -> float:
        marker = self.dest_base / agent / ".last-sync"
        if marker.is_file():
            try:
                return float(marker.read_text().strip())
            except (ValueError, OSError):
                pass
        return 0.0

    def set_last_sync(self, agent: str) -> None:
        if self.dry_run:
            return
        marker_dir = self.dest_base / agent
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / ".last-sync").write_text(str(time.time()))


# ---------------------------------------------------------------------------
# SkillIndexBuilder — parse synced copies, build INDEX.jsonl
# ---------------------------------------------------------------------------


class SkillIndexBuilder:
    """Parse synced skill copies and produce INDEX.jsonl."""

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir

    def build(self, agents: list[str] | None = None) -> list[SkillEntry]:
        target_agents = agents or list(SKILL_SOURCES.keys())
        entries: list[SkillEntry] = []

        for agent in target_agents:
            agent_dir = self.skills_dir / agent
            if not agent_dir.is_dir():
                continue

            # Scan type directories
            for type_dir in sorted(agent_dir.iterdir()):
                if not type_dir.is_dir() or type_dir.name.startswith("."):
                    continue

                type_name = type_dir.name
                # Map plural dir names back to singular types
                type_map = {
                    "skills": "skill",
                    "commands": "command",
                    "plugins": "plugin",
                    "automations": "automation",
                    "rules": "rule",
                    "config": "config",
                }
                item_type = type_map.get(type_name, type_name)

                if item_type == "config":
                    # Config files are directly in config/
                    for config_file in sorted(type_dir.iterdir()):
                        if not config_file.is_file():
                            continue
                        entry = self._parse_config(agent, config_file)
                        if entry:
                            entries.append(entry)
                elif item_type == "command":
                    # Commands are individual files
                    for cmd_file in sorted(type_dir.iterdir()):
                        if not cmd_file.is_file() or cmd_file.suffix != ".md":
                            continue
                        entry = self._parse_command(agent, cmd_file)
                        if entry:
                            entries.append(entry)
                elif item_type == "rule":
                    # Rules directory treated as single item
                    entry = self._parse_rule(agent, type_dir)
                    if entry:
                        entries.append(entry)
                else:
                    # Skills, plugins, automations are directories
                    for item_dir in sorted(type_dir.iterdir()):
                        if not item_dir.is_dir():
                            continue
                        # Handle .system/ nesting for codex
                        if item_dir.name == ".system":
                            for sys_dir in sorted(item_dir.iterdir()):
                                if sys_dir.is_dir():
                                    entry = self._parse_dir_item(
                                        agent,
                                        item_type,
                                        sys_dir,
                                        name_prefix=".system/",
                                    )
                                    if entry:
                                        entries.append(entry)
                        else:
                            entry = self._parse_dir_item(
                                agent,
                                item_type,
                                item_dir,
                            )
                            if entry:
                                entries.append(entry)

        entries.sort(key=lambda e: (e.agent, e.type, e.name))
        return entries

    def write_index(self, entries: list[SkillEntry]) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = INDEX_FILE.with_suffix(".jsonl.tmp")
        with tmp_path.open("w") as f:
            for entry in entries:
                f.write(json.dumps(entry.to_dict(), separators=(",", ":")) + "\n")
        os.replace(tmp_path, INDEX_FILE)

    def _parse_dir_item(
        self,
        agent: str,
        item_type: str,
        item_dir: Path,
        name_prefix: str = "",
    ) -> SkillEntry | None:
        name = name_prefix + item_dir.name
        item_id = f"{agent}:{item_type}:{name}"

        files = [
            str(f.relative_to(item_dir))
            for f in sorted(item_dir.rglob("*"))
            if f.is_file()
        ]
        if not files:
            return None

        metadata: dict[str, Any] = {}

        if item_type == "skill":
            fm = _parse_frontmatter(item_dir / "SKILL.md")
            description = fm.get("description", "")
            metadata["frontmatter"] = fm
            metadata["has_agents"] = (item_dir / "agents").is_dir()
            metadata["has_scripts"] = (item_dir / "scripts").is_dir()
            if fm.get("system") == "true":
                metadata["system"] = True

        elif item_type == "plugin":
            pjson_path = item_dir / "plugin.json"
            if not pjson_path.is_file():
                pjson_path = item_dir / ".claude-plugin" / "plugin.json"
            pjson = _safe_read_json(pjson_path)
            fm = _parse_frontmatter(item_dir / "SKILL.md")
            description = pjson.get("description", "") or fm.get("description", "")
            if pjson:
                metadata["plugin_json"] = {
                    k: v
                    for k, v in pjson.items()
                    if k in ("name", "description", "version", "author")
                }
            # Discover contained items
            agents_dir = item_dir / "agents"
            cmds_dir = item_dir / "commands"
            metadata["contained_agents"] = (
                [f.stem for f in sorted(agents_dir.iterdir()) if f.suffix == ".md"]
                if agents_dir.is_dir()
                else []
            )
            metadata["contained_commands"] = (
                [f.stem for f in sorted(cmds_dir.iterdir()) if f.suffix == ".md"]
                if cmds_dir.is_dir()
                else []
            )
            metadata["has_skill"] = (item_dir / "SKILL.md").is_file()

        elif item_type == "automation":
            toml_data = _parse_simple_toml(item_dir / "automation.toml")
            description = str(toml_data.get("name", name))
            metadata["status"] = str(toml_data.get("status", ""))
            if toml_data.get("rrule"):
                metadata["schedule"] = str(toml_data["rrule"])
            if toml_data.get("prompt"):
                prompt = str(toml_data["prompt"])
                metadata["prompt"] = (
                    prompt[:120] + "..." if len(prompt) > 120 else prompt
                )

        else:
            description = ""

        synced_rel = f"{agent}/{item_type}s/{name}/"
        source_path = _collapse_home(
            SKILL_SOURCES[agent].source_dir
            / (
                "skills"
                if item_type == "skill"
                else "automations"
                if item_type == "automation"
                else "plugins/marketplaces/claude-plugins-official/plugins"
            )
            / name
        )

        return SkillEntry(
            id=item_id,
            agent=agent,
            type=item_type,
            name=name,
            description=description,
            source_path=source_path,
            synced_path=synced_rel,
            files=files,
            metadata=metadata,
        )

    def _parse_command(self, agent: str, cmd_file: Path) -> SkillEntry | None:
        name = cmd_file.stem
        fm = _parse_frontmatter(cmd_file)
        description = fm.get("description", "")

        metadata: dict[str, Any] = {"frontmatter": fm}
        if fm.get("argument-hint"):
            metadata["argument_hint"] = fm["argument-hint"]
        if fm.get("allowed-tools"):
            metadata["allowed_tools"] = fm["allowed-tools"]
        if fm.get("model"):
            metadata["model"] = fm["model"]

        return SkillEntry(
            id=f"{agent}:command:{name}",
            agent=agent,
            type="command",
            name=name,
            description=description,
            source_path=_collapse_home(
                SKILL_SOURCES[agent].source_dir / "commands" / cmd_file.name
            ),
            synced_path=f"{agent}/commands/{cmd_file.name}",
            files=[cmd_file.name],
            metadata=metadata,
        )

    def _parse_config(self, agent: str, config_file: Path) -> SkillEntry | None:
        name = config_file.name
        metadata: dict[str, Any] = {}

        if config_file.suffix == ".json":
            data = _safe_read_json(config_file)
            # Extract key settings only
            key_settings: dict[str, Any] = {}
            for k in ("model", "theme", "banner", "personality"):
                if k in data:
                    key_settings[k] = data[k]
            if data.get("trusted_folders"):
                key_settings["trusted_folders"] = data["trusted_folders"]
            metadata["key_settings"] = key_settings

        elif config_file.suffix == ".toml":
            data = _parse_simple_toml(config_file)
            key_settings = {}
            for k in ("model", "model_reasoning_effort", "personality"):
                if k in data:
                    key_settings[k] = data[k]
            metadata["key_settings"] = key_settings

        return SkillEntry(
            id=f"{agent}:config:{name}",
            agent=agent,
            type="config",
            name=name,
            description=f"{agent} configuration",
            source_path=_collapse_home(SKILL_SOURCES[agent].source_dir / name),
            synced_path=f"{agent}/config/{name}",
            files=[name],
            metadata=metadata,
        )

    def _parse_rule(self, agent: str, rules_dir: Path) -> SkillEntry | None:
        files = [
            str(f.relative_to(rules_dir))
            for f in sorted(rules_dir.rglob("*"))
            if f.is_file()
        ]
        if not files:
            return None

        # Count rules
        rule_count = 0
        for f in rules_dir.rglob("*"):
            if f.is_file():
                try:
                    rule_count += sum(
                        1
                        for line in f.read_text(errors="replace").splitlines()
                        if line.strip() and not line.startswith("#")
                    )
                except OSError:
                    pass

        return SkillEntry(
            id=f"{agent}:rule:rules",
            agent=agent,
            type="rule",
            name="rules",
            description=f"{rule_count} rules",
            source_path=_collapse_home(SKILL_SOURCES[agent].source_dir / "rules"),
            synced_path=f"{agent}/rules/",
            files=files,
            metadata={"rule_count": rule_count},
        )


# ---------------------------------------------------------------------------
# VaultFeeder — copy portable skills/commands into vault
# ---------------------------------------------------------------------------


class VaultFeeder:
    """Copy portable skills and commands into the vault for redistribution."""

    def __init__(self, vault_dir: Path, dry_run: bool = False) -> None:
        self.vault_dir = vault_dir
        self.dry_run = dry_run

    def feed(self, items: list[DiscoveredItem]) -> tuple[int, int]:
        """Feed portable items into vault. Returns (copied, skipped)."""
        total_copied = 0
        total_skipped = 0

        for item in items:
            if item.item_type == "skill":
                c, s = self._feed_skill(item)
                total_copied += c
                total_skipped += s
            elif item.item_type == "command":
                c, s = self._feed_command(item)
                total_copied += c
                total_skipped += s

        return total_copied, total_skipped

    def _feed_skill(self, item: DiscoveredItem) -> tuple[int, int]:
        # Codex skills get agent suffix
        if item.agent == "codex":
            vault_name = f"{item.name}.codex"
        else:
            vault_name = item.name

        dest_dir = self.vault_dir / "skills" / vault_name
        return self._copy_files(item.files, dest_dir)

    def _feed_command(self, item: DiscoveredItem) -> tuple[int, int]:
        dest_dir = self.vault_dir / "commands"
        return self._copy_files(item.files, dest_dir)

    def _copy_files(
        self,
        files: list[tuple[Path, Path]],
        dest_dir: Path,
    ) -> tuple[int, int]:
        copied = 0
        skipped = 0

        for source_abs, dest_rel in files:
            dest_file = dest_dir / dest_rel

            if dest_file.is_file():
                try:
                    if dest_file.stat().st_mtime >= source_abs.stat().st_mtime:
                        skipped += 1
                        continue
                except OSError:
                    pass

            if not self.dry_run:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_abs, dest_file)
            copied += 1

        return copied, skipped


# ---------------------------------------------------------------------------
# SkillCleaner — remove synced data
# ---------------------------------------------------------------------------


class SkillCleaner:
    """Remove synced skill data."""

    def __init__(self, skills_dir: Path, dry_run: bool = False) -> None:
        self.skills_dir = skills_dir
        self.dry_run = dry_run

    def clean(self, agent: str | None = None) -> None:
        agents = [agent] if agent else list(SKILL_SOURCES.keys())
        for a in agents:
            agent_dir = self.skills_dir / a
            if agent_dir.is_dir():
                count = sum(
                    1 for d in agent_dir.iterdir() if not d.name.startswith(".")
                )
                print(f"  [{a}] Removing {count} synced items")
                if not self.dry_run:
                    shutil.rmtree(agent_dir)

        if agent is None and INDEX_FILE.is_file():
            print("  Removing INDEX.jsonl")
            if not self.dry_run:
                INDEX_FILE.unlink()


# ---------------------------------------------------------------------------
# SkillsSync — orchestrator
# ---------------------------------------------------------------------------


class SkillsSync:
    """Orchestrator: coordinates discovery, copy, indexing, vault feed, and cleanup."""

    def __init__(
        self,
        skills_dir: Path = SKILLS_DIR,
        vault_dir: Path = VAULT_DIR,
        dry_run: bool = False,
    ) -> None:
        self.skills_dir = skills_dir
        self.vault_dir = vault_dir
        self.dry_run = dry_run
        self._discovery = SkillDiscovery()
        self._copier = SkillCopier(skills_dir, dry_run=dry_run)
        self._indexer = SkillIndexBuilder(skills_dir)
        self._feeder = VaultFeeder(vault_dir, dry_run=dry_run)
        self._cleaner = SkillCleaner(skills_dir, dry_run=dry_run)

    def sync_all(
        self,
        agent: str | None = None,
        force: bool = False,
    ) -> None:
        agents = [agent] if agent else list(SKILL_SOURCES.keys())
        total_copied = 0
        total_skipped = 0
        all_items: list[DiscoveredItem] = []

        for agent_name in agents:
            source = SKILL_SOURCES.get(agent_name)
            if source is None:
                print(f"  Unknown agent: {agent_name}", file=sys.stderr)
                continue

            if not source.source_dir.is_dir():
                print(f"  [{agent_name}] Source not found: {source.source_dir}")
                continue

            print(f"\nDiscovering {agent_name} skills & tools...")
            items = self._discovery.discover(source)

            # Type counts
            type_counts: dict[str, int] = {}
            for item in items:
                type_counts[item.item_type] = type_counts.get(item.item_type, 0) + 1
            parts = [f"{c} {t}s" for t, c in sorted(type_counts.items())]
            print(f"  [{agent_name}] Found: {', '.join(parts) or 'nothing'}")

            agent_copied = 0
            agent_skipped = 0

            for item in items:
                c, s = self._copier.sync_item(item, force=force)
                agent_copied += c
                agent_skipped += s

            total_copied += agent_copied
            total_skipped += agent_skipped
            all_items.extend(items)

            self._copier.set_last_sync(agent_name)
            print(
                f"  [{agent_name}] {agent_copied} files copied, "
                f"{agent_skipped} unchanged"
            )

        # Build index
        print("\nBuilding skills index...")
        entries = self._indexer.build()
        if not self.dry_run:
            self._indexer.write_index(entries)
        print(f"  INDEX.jsonl: {len(entries)} items indexed")

        # Feed vault
        portable = [i for i in all_items if i.item_type in ("skill", "command")]
        if portable:
            print(f"\nFeeding vault ({len(portable)} portable items)...")
            vc, vs = self._feeder.feed(portable)
            print(f"  Vault: {vc} files copied, {vs} unchanged")

        print(
            f"\nSync complete. {total_copied} files copied, {total_skipped} unchanged."
        )

    def rebuild_index(self, agent: str | None = None) -> None:
        agents_filter = [agent] if agent else None
        print("Rebuilding skills index...")
        entries = self._indexer.build(agents=agents_filter)
        if not self.dry_run:
            self._indexer.write_index(entries)
        print(f"  INDEX.jsonl: {len(entries)} items indexed")

    def clean(self, agent: str | None = None) -> None:
        print("Cleaning synced skill data...")
        self._cleaner.clean(agent=agent)
        print("Clean complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Skills & tools sync — discover, copy, index, and redistribute",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 skills_sync.py                          # Sync all agents
  python3 skills_sync.py --agent claude           # Sync claude only
  python3 skills_sync.py --dry-run                # Preview changes
  python3 skills_sync.py --clean                  # Remove synced data
  python3 skills_sync.py --force                  # Force re-copy all
  python3 skills_sync.py --rebuild-index          # Regen INDEX.jsonl only
        """,
    )
    parser.add_argument(
        "--agent",
        choices=["claude", "copilot", "codex"],
        help="Sync specific agent only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without changes",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove all synced skill data",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-copy all (ignore mtime)",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Regenerate INDEX.jsonl from synced copies",
    )

    args = parser.parse_args()
    sync = SkillsSync(dry_run=args.dry_run)

    if args.dry_run:
        print("DRY RUN — no changes will be made\n")

    if args.clean:
        sync.clean(agent=args.agent)
    elif args.rebuild_index:
        sync.rebuild_index(agent=args.agent)
    else:
        sync.sync_all(agent=args.agent, force=args.force)


if __name__ == "__main__":
    main()
