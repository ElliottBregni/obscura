"""obscura.plugins.claude_compat.skill_loader — Load Claude Code skills as slash commands.

Claude Code skills are ``SKILL.md`` files with YAML frontmatter and
markdown body.  This module parses them and registers each as an Obscura
slash command that injects the skill content as a prompt to the agent.

Naming convention: ``/plugin-name:skill-name`` (matches Claude Code).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from obscura.plugins.claude_compat.variables import substitute_variables

logger = logging.getLogger(__name__)


def load_skills_as_commands(
    plugin_dir: Path,
    plugin_name: str,
    *,
    plugin_data: Path | None = None,
    user_config: dict[str, str] | None = None,
) -> dict[str, SkillCommand]:
    """Discover and parse skills from a Claude Code plugin.

    Searches both ``skills/`` (subdirectory layout) and ``commands/``
    (flat layout) directories.

    Returns a dict mapping command name (``plugin:skill``) to
    :class:`SkillCommand` instances ready for registration.
    """
    commands: dict[str, SkillCommand] = {}

    if plugin_data is None:
        from obscura.plugins.claude_compat.variables import get_plugin_data_dir

        plugin_data = get_plugin_data_dir(plugin_name)

    # Skills directory: skills/<name>/SKILL.md
    skills_dir = plugin_dir / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = f"{plugin_name}:{skill_dir.name}"
            cmd = _parse_skill(
                skill_file,
                name=name,
                plugin_root=plugin_dir,
                plugin_data=plugin_data,
                skill_dir=skill_dir,
                user_config=user_config,
            )
            if cmd:
                commands[name] = cmd

    # Commands directory: commands/<name>.md (flat) or commands/<dir>/<name>.md
    commands_dir = plugin_dir / "commands"
    if commands_dir.is_dir():
        for md_file in sorted(commands_dir.rglob("*.md")):
            rel = md_file.relative_to(commands_dir)
            parts = list(rel.with_suffix("").parts)
            slug = ":".join(parts)
            name = f"{plugin_name}:{slug}"
            cmd = _parse_skill(
                md_file,
                name=name,
                plugin_root=plugin_dir,
                plugin_data=plugin_data,
                user_config=user_config,
            )
            if cmd:
                commands[name] = cmd

    return commands


class SkillCommand:
    """A Claude Code skill parsed into an Obscura-compatible command.

    When invoked, injects the skill's markdown body as a prompt to the
    current agent, respecting frontmatter settings (model, effort, tools).
    """

    def __init__(
        self,
        name: str,
        body: str,
        *,
        description: str = "",
        model: str = "",
        effort: str = "",
        allowed_tools: list[str] | None = None,
        user_invocable: bool = True,
        disable_model_invocation: bool = False,
        argument_hint: str = "",
    ) -> None:
        self.name = name
        self.body = body
        self.description = description
        self.model = model
        self.effort = effort
        self.allowed_tools = allowed_tools
        self.user_invocable = user_invocable
        self.disable_model_invocation = disable_model_invocation
        self.argument_hint = argument_hint

    def build_prompt(self, args: str = "") -> str:
        """Build the prompt to inject when this skill is invoked.

        If *args* is provided, it's appended after the skill body.
        """
        prompt = self.body
        if args:
            prompt = f"{prompt}\n\nUser input: {args}"
        return prompt


def _parse_skill(
    path: Path,
    *,
    name: str,
    plugin_root: Path,
    plugin_data: Path,
    skill_dir: Path | None = None,
    user_config: dict[str, str] | None = None,
) -> SkillCommand | None:
    """Parse a single SKILL.md or command .md file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        logger.debug("Could not read skill file %s", path, exc_info=True)
        return None

    frontmatter, body = _split_frontmatter(raw)

    # Substitute variables in body.
    body = substitute_variables(
        body,
        plugin_root=plugin_root,
        plugin_data=plugin_data,
        skill_dir=skill_dir,
        user_config=user_config,
    )

    # Parse frontmatter.
    meta: dict[str, Any] = {}
    if frontmatter:
        try:
            meta = yaml.safe_load(frontmatter) or {}
        except Exception:
            logger.debug("Invalid YAML frontmatter in %s", path)

    description = str(meta.get("description", ""))
    model = str(meta.get("model", ""))
    effort = str(meta.get("effort", ""))

    # Allowed tools: string (comma-separated) or list.
    allowed_tools_raw = meta.get("allowed-tools") or meta.get("allowedTools")
    allowed_tools: list[str] | None = None
    if isinstance(allowed_tools_raw, str):
        allowed_tools = [t.strip() for t in allowed_tools_raw.split(",") if t.strip()]
    elif isinstance(allowed_tools_raw, list):
        allowed_tools = [str(t) for t in allowed_tools_raw]

    user_invocable = meta.get("user-invocable", True)
    disable_model = meta.get("disable-model-invocation", False)
    argument_hint = str(meta.get("argument-hint", meta.get("argumentHint", "")))

    return SkillCommand(
        name=name,
        body=body.strip(),
        description=description,
        model=model,
        effort=effort,
        allowed_tools=allowed_tools,
        user_invocable=bool(user_invocable),
        disable_model_invocation=bool(disable_model),
        argument_hint=argument_hint,
    )


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """Split ``---`` delimited YAML frontmatter from body."""
    if not raw.startswith("---"):
        return "", raw
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", raw, re.DOTALL)
    if match:
        return match.group(1), match.group(2)
    return "", raw
