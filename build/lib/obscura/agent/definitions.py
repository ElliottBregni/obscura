"""obscura.agent.definitions — Agent definition model, loader, and resolver.

Agent definitions are markdown files with TOML/YAML frontmatter that
describe specialized agent types (tools, model, system prompt, etc.).

Discovery order (later overrides earlier):
  1. Built-in definitions (``obscura/agent/builtin/*.md``)
  2. Global user definitions (``~/.obscura/agents/*.md``)
  3. Local project definitions (``.obscura/agents/*.md``)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obscura.core.frontmatter import parse_frontmatter_file

logger = logging.getLogger(__name__)

_BUILTIN_DIR = Path(__file__).parent / "builtin"


def _str_list(val: Any) -> list[str]:
    """Coerce a value to a list of strings."""
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        return [s.strip() for s in val.split(",") if s.strip()]
    return []


@dataclass(frozen=True)
class AgentDefinition:
    """Immutable agent type definition parsed from a markdown file."""

    name: str
    description: str = ""
    system_prompt: str = ""
    tools: tuple[str, ...] = ()  # allowlist (empty = all tools)
    disallowed_tools: tuple[str, ...] = ()  # denylist
    model: str = "inherit"  # "inherit" or specific model ID
    max_turns: int = 50
    permission_mode: str = "default"
    isolation: str = ""  # "" or "worktree"
    background: bool = False
    source: str = "built-in"  # "built-in", "global", "local"


def load_agent_definition(path: Path, *, source: str = "local") -> AgentDefinition:
    """Parse a single ``.md`` agent definition file."""
    result = parse_frontmatter_file(path)
    meta = result.metadata

    return AgentDefinition(
        name=str(meta.get("name", path.stem)),
        description=str(meta.get("description", "")),
        system_prompt=result.body.strip(),
        tools=tuple(_str_list(meta.get("tools", []))),
        disallowed_tools=tuple(_str_list(meta.get("disallowed_tools", []))),
        model=str(meta.get("model", "inherit")),
        max_turns=int(meta.get("max_turns", 50)),
        permission_mode=str(meta.get("permission_mode", "default")),
        isolation=str(meta.get("isolation", "")),
        background=bool(meta.get("background", False)),
        source=source,
    )


def load_definitions_dir(
    directory: Path,
    *,
    source: str = "local",
) -> dict[str, AgentDefinition]:
    """Scan a directory for ``.md`` agent definition files."""
    defs: dict[str, AgentDefinition] = {}
    if not directory.is_dir():
        return defs
    for path in sorted(directory.glob("*.md")):
        try:
            defn = load_agent_definition(path, source=source)
            defs[defn.name] = defn
        except Exception:
            logger.warning("Failed to load agent definition: %s", path, exc_info=True)
    return defs


def resolve_all_definitions(cwd: Path | None = None) -> dict[str, AgentDefinition]:
    """Resolve all agent definitions in merge order.

    Later sources override earlier ones by name:
      1. Built-in (lowest priority)
      2. Global ``~/.obscura/agents/``
      3. Local ``.obscura/agents/`` (highest priority)
    """
    merged: dict[str, AgentDefinition] = {}

    # 1. Built-in definitions.
    merged.update(load_definitions_dir(_BUILTIN_DIR, source="built-in"))

    # 2. Global user definitions.
    from obscura.core.paths import resolve_obscura_global_home

    global_agents = resolve_obscura_global_home() / "agents"
    merged.update(load_definitions_dir(global_agents, source="global"))

    # 3. Local project definitions.
    working_dir = (cwd or Path.cwd()).resolve()
    local_agents = working_dir / ".obscura" / "agents"
    if local_agents != global_agents:
        merged.update(load_definitions_dir(local_agents, source="local"))

    return merged


def definition_to_config_dict(
    defn: AgentDefinition,
    parent_model: str = "copilot",
) -> dict[str, Any]:
    """Convert an ``AgentDefinition`` to a dict suitable for ``AgentConfig``."""
    model = parent_model if defn.model == "inherit" else defn.model
    return {
        "name": defn.name,
        "provider": model,
        "system_prompt": defn.system_prompt,
        "max_iterations": defn.max_turns,
        "max_turns": defn.max_turns,
        "tool_allowlist": list(defn.tools) if defn.tools else None,
    }
