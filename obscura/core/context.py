"""
obscura.context — Load role-specific prompts and context from vault directories.

Reads from the synced directories created by ``sync.py``::

    ~/.github/instructions/   (copilot)
    ~/.github/skills/         (copilot)
    ~/.claude/instructions/   (claude)
    ~/.claude/skills/         (claude)

Usage::

    loader = ContextLoader(Backend.COPILOT)
    system_prompt = loader.load_system_prompt()
    skills = loader.load_skills()
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from obscura.core.frontmatter import parse_frontmatter
from obscura.core.types import Backend

from typing import cast


# Agent target mapping — must match sync.py AGENT_TARGET_MAP
_DEFAULT_TARGET_MAP: dict[str, str] = {
    "copilot": ".github",
    "claude": ".claude",
    "cursor": ".cursor",
}


class ContextLoader:
    """Load instructions, skills, and role context from vault directories."""

    def __init__(
        self,
        backend: Backend,
        vault_path: Path | None = None,
        agent_target_map: dict[str, str] | None = None,
    ) -> None:
        self._backend = backend
        self._vault_path = vault_path or Path.home()
        self._target_map = agent_target_map or _DEFAULT_TARGET_MAP

    @property
    def agent_dir(self) -> Path:
        """Root directory for this agent (e.g. ``~/.github/``)."""
        target = self._target_map.get(self._backend.value, f".{self._backend.value}")
        return self._vault_path / target

    def load_instructions(self) -> str:
        """Load all instruction files, concatenated with separators."""
        instructions_dir = self.agent_dir / "instructions"
        if not instructions_dir.is_dir():
            return ""
        parts: list[str] = []
        for f in sorted(instructions_dir.rglob("*.md")):
            if f.is_file():
                text = f.read_text(encoding="utf-8").strip()
                if text:
                    parts.append(text)
        return "\n\n---\n\n".join(parts)

    def load_skills(self) -> list[str]:
        """Load skill documents as a list of strings."""
        skills_dir = self.agent_dir / "skills"
        if not skills_dir.is_dir():
            return []
        return [
            f.read_text(encoding="utf-8").strip()
            for f in sorted(skills_dir.rglob("*.md"))
            if f.is_file() and f.read_text(encoding="utf-8").strip()
        ]

    def load_role(self, name: str) -> str:
        """Load role-specific context from ``skills/roles/{name}/``."""
        role_dir = self.agent_dir / "skills" / "roles" / name
        if not role_dir.is_dir():
            return ""
        parts: list[str] = []
        for f in sorted(role_dir.rglob("*.md")):
            if f.is_file():
                text = f.read_text(encoding="utf-8").strip()
                if text:
                    parts.append(text)
        return "\n\n".join(parts)

    def load_claude_md(self) -> str:
        """Load CLAUDE.md from the agent root dir (e.g. ~/.claude/CLAUDE.md)."""
        f = self.agent_dir / "CLAUDE.md"
        if f.is_file():
            return f.read_text(encoding="utf-8").strip()
        return ""

    def load_instructions_filtered(self, file_context: str = "") -> str:
        """Load instruction files, filtering by ``applyTo`` frontmatter globs.

        If an instruction file has an ``applyTo`` field in its frontmatter,
        it is only included when *file_context* matches one of the listed
        glob patterns.  Files without ``applyTo`` are always included.
        """
        instructions_dir = self.agent_dir / "instructions"
        if not instructions_dir.is_dir():
            return ""
        parts: list[str] = []
        for f in sorted(instructions_dir.rglob("*.md")):
            if not f.is_file():
                continue
            raw = f.read_text(encoding="utf-8").strip()
            if not raw:
                continue
            result = parse_frontmatter(raw, source_path=f)
            apply_to_raw: Any = result.metadata.get("applyTo", result.metadata.get("apply_to"))

            if apply_to_raw and file_context:
                patterns: list[str] = []
                if isinstance(apply_to_raw, str):
                    patterns = [p.strip() for p in apply_to_raw.split(",") if p.strip()]
                elif isinstance(apply_to_raw, list):
                    patterns = [str(p) for p in cast("list[Any]", apply_to_raw)]
                if patterns and not any(fnmatch.fnmatch(file_context, p) for p in patterns):
                    continue

            body = result.body.strip()
            if body:
                parts.append(body)
        return "\n\n---\n\n".join(parts)

    def load_skills_with_metadata(self) -> list[tuple[dict[str, Any], str]]:
        """Load skill documents as ``(metadata, body)`` tuples.

        If a skill file has YAML frontmatter, ``metadata`` will contain
        the parsed fields (e.g. ``name``, ``description``, ``allowed-tools``).
        """
        skills_dir = self.agent_dir / "skills"
        if not skills_dir.is_dir():
            return []
        results: list[tuple[dict[str, Any], str]] = []
        for f in sorted(skills_dir.rglob("*.md")):
            if not f.is_file():
                continue
            raw = f.read_text(encoding="utf-8").strip()
            if not raw:
                continue
            result = parse_frontmatter(raw, source_path=f)
            results.append((result.metadata, result.body))
        return results

    def load_system_prompt(self, additional: str = "") -> str:
        """Build a system prompt from CLAUDE.md + instructions + skills + optional extra."""
        parts: list[str] = []
        claude_md = self.load_claude_md()
        if claude_md:
            parts.append(claude_md)
        instructions = self.load_instructions()
        if instructions:
            parts.append(instructions)
        skills = self.load_skills()
        if skills:
            parts.append("## Skills\n\n" + "\n\n".join(skills))
        if additional:
            parts.append(additional)
        return "\n\n".join(parts)
