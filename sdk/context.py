"""
sdk.context — Load role-specific prompts and context from vault directories.

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

from pathlib import Path

from sdk._types import Backend


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

    def load_system_prompt(self, additional: str = "") -> str:
        """Build a system prompt from instructions + skills + optional extra."""
        parts: list[str] = []
        instructions = self.load_instructions()
        if instructions:
            parts.append(instructions)
        skills = self.load_skills()
        if skills:
            parts.append("## Skills\n\n" + "\n\n".join(skills))
        if additional:
            parts.append(additional)
        return "\n\n".join(parts)
