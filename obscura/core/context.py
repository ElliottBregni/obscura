"""obscura.context — Load role-specific prompts and context from vault directories.

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
import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from obscura.core.context_lazy import LazySkillLoader, SkillMetadata
from obscura.core.frontmatter import parse_frontmatter
from obscura.core.paths import resolve_all_skills_dirs
from obscura.core.enums.agent import Role
from obscura.core.types import ContentBlock, Message

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from obscura.core.enums.agent import Backend

# Agent target mapping — must match sync.py AGENT_TARGET_MAP
_DEFAULT_TARGET_MAP: dict[str, str] = {
    "copilot": ".github",
    "claude": ".claude",
    "cursor": ".cursor",
}


class ContextLoader:
    """Load instructions, skills, and role context from vault directories.

    Skills are aggregated from the backend-specific dir (e.g. ``.codex/skills/``)
    and the universal pool returned by :func:`resolve_all_skills_dirs`
    (``~/.obscura/skills/``, ``.obscura/skills/``, ``~/.claude/skills/``), so
    every backend sees the same baseline skill set with optional per-backend
    additions. Backend-specific entries win on name conflicts.
    """

    def __init__(
        self,
        backend: Backend,
        vault_path: Path | None = None,
        agent_target_map: dict[str, str] | None = None,
        lazy_load_skills: bool = False,
        skill_filter: list[str] | None = None,
        capability_resolver: Any = None,
        agent_id: str = "",
    ) -> None:
        self._backend = backend
        self._vault_path = vault_path or Path.home()
        self._target_map = agent_target_map or _DEFAULT_TARGET_MAP
        self._lazy_load_skills = lazy_load_skills
        self._skill_filter = skill_filter
        self._lazy_loaders: list[LazySkillLoader] | None = None
        self._capability_resolver = capability_resolver
        self._agent_id = agent_id

    @property
    def agent_dir(self) -> Path:
        """Root directory for this agent (e.g. ``~/.github/``)."""
        target = self._target_map.get(self._backend.value, f".{self._backend.value}")
        return self._vault_path / target

    def _skill_dirs(self) -> list[Path]:
        """All skill directories to search, in priority order (first wins).

        Backend-specific (``agent_dir/skills/``) is searched first so that
        a same-named entry there overrides one in the universal pool.
        """
        dirs: list[Path] = []
        seen: set[Path] = set()

        backend_dir = self.agent_dir / "skills"
        if backend_dir.is_dir():
            resolved = backend_dir.resolve()
            dirs.append(backend_dir)
            seen.add(resolved)

        for d in resolve_all_skills_dirs():
            resolved = d.resolve()
            if resolved not in seen:
                dirs.append(d)
                seen.add(resolved)

        return dirs

    def _ensure_lazy_loaders(self) -> list[LazySkillLoader]:
        """Lazily create one LazySkillLoader per discovered skill dir.

        Each loader is pre-populated with its metadata cache so subsequent
        :meth:`load_skill_body` calls skip the disk walk.
        """
        if self._lazy_loaders is None:
            loaders = [LazySkillLoader(d) for d in self._skill_dirs()]
            for loader in loaders:
                loader.discover_skills(filter_names=self._skill_filter)
            self._lazy_loaders = loaders
        return self._lazy_loaders

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
        """Load skill documents as a list of strings.

        Aggregates across :meth:`_skill_dirs`, deduplicating by file stem so
        the highest-priority dir wins. Returns empty list if
        ``lazy_load_skills`` is enabled (use :meth:`load_skills_lazy` instead).
        """
        if self._lazy_load_skills:
            return []  # Don't eagerly load skills

        out: list[str] = []
        seen: set[str] = set()
        for skills_dir in self._skill_dirs():
            for f in sorted(skills_dir.rglob("*.md")):
                if not f.is_file() or f.stem in seen:
                    continue
                text = f.read_text(encoding="utf-8").strip()
                if not text:
                    continue
                out.append(text)
                seen.add(f.stem)
        return out

    def load_skills_lazy(self) -> list[SkillMetadata]:
        """Discover skill metadata across all skill dirs.

        Returns:
            List of skill metadata objects, deduplicated by name with
            higher-priority dirs winning.

        """
        # _ensure_lazy_loaders pre-populates each loader's metadata cache,
        # so we can read from it directly instead of re-walking the disk.
        seen: set[str] = set()
        skills: list[SkillMetadata] = []
        for loader in self._ensure_lazy_loaders():
            for s in loader.discovered_skills():
                if s.name in seen:
                    continue
                skills.append(s)
                seen.add(s.name)

        if self._capability_resolver is not None and self._agent_id:
            skills = [
                s
                for s in skills
                if self._capability_resolver.is_granted(
                    self._agent_id,
                    f"skill.{s.name.replace('-', '_')}",
                )
            ]

        return skills

    def load_skill_body(self, skill_name: str) -> str | None:
        """Load full skill body on-demand.

        Searches each skill dir in priority order; the first hit wins.

        Args:
            skill_name: Name of the skill to load

        Returns:
            Full skill content, or None if not found

        """
        for loader in self._ensure_lazy_loaders():
            body = loader.load_skill_body(skill_name)
            if body is not None:
                return body
        return None

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

    def load_project_instructions(self) -> str:
        """Load project instructions from OBSCURA.md (or CLAUDE.md fallback)."""
        for name in ("OBSCURA.md", "CLAUDE.md"):
            f = self.agent_dir / name
            if f.is_file():
                return f.read_text(encoding="utf-8").strip()
        return ""

    # Backwards-compatible alias.
    load_claude_md = load_project_instructions

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
            apply_to_raw: Any = result.metadata.get(
                "applyTo",
                result.metadata.get("apply_to"),
            )

            if apply_to_raw and file_context:
                patterns: list[str] = []
                if isinstance(apply_to_raw, str):
                    patterns = [p.strip() for p in apply_to_raw.split(",") if p.strip()]
                elif isinstance(apply_to_raw, list):
                    patterns = [str(p) for p in cast("list[Any]", apply_to_raw)]
                if patterns and not any(
                    fnmatch.fnmatch(file_context, p) for p in patterns
                ):
                    continue

            body = result.body.strip()
            if body:
                parts.append(body)
        return "\n\n---\n\n".join(parts)

    def load_skills_with_metadata(self) -> list[tuple[dict[str, Any], str]]:
        """Load skill documents as ``(metadata, body)`` tuples.

        Aggregates across :meth:`_skill_dirs`, deduplicating by frontmatter
        ``name`` (or file stem if absent). If a skill file has YAML
        frontmatter, ``metadata`` will contain the parsed fields
        (e.g. ``name``, ``description``, ``allowed-tools``).
        """
        results: list[tuple[dict[str, Any], str]] = []
        seen: set[str] = set()
        for skills_dir in self._skill_dirs():
            for f in sorted(skills_dir.rglob("*.md")):
                if not f.is_file():
                    continue
                raw = f.read_text(encoding="utf-8").strip()
                if not raw:
                    continue
                result = parse_frontmatter(raw, source_path=f)
                key = str(result.metadata.get("name") or f.stem)
                if key in seen:
                    continue
                seen.add(key)
                results.append((result.metadata, result.body))
        return results

    def load_system_prompt(self, additional: str = "") -> str:
        """Build a system prompt from OBSCURA.md + instructions + skills + optional extra.

        If lazy_load_skills is enabled, only includes skill stubs (name + description).
        """
        parts: list[str] = []
        project_instructions = self.load_project_instructions()
        if project_instructions:
            parts.append(project_instructions)
        instructions = self.load_instructions()
        if instructions:
            parts.append(instructions)

        # Handle skills (lazy or eager)
        if self._lazy_load_skills:
            skill_metas = self.load_skills_lazy()
            if skill_metas:
                stubs = "\n\n".join(s.to_stub() for s in skill_metas)
                if stubs:
                    parts.append("## Skills (Available)\n\n" + stubs)
        else:
            skills = self.load_skills()
            if skills:
                parts.append("## Skills\n\n" + "\n\n".join(skills))

        if additional:
            parts.append(additional)
        return "\n\n".join(parts)


def load_obscura_memory(session_id: str, db_path: Path, max_events: int = 50) -> str:
    """Load recent events from .obscura/events.db as memory context.

    Args:
        session_id: Session ID to load events for
        db_path: Path to events.db
        max_events: Maximum number of recent events to include

    Returns:
        Formatted memory context string for system prompt

    """
    if not db_path.exists():
        return ""

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Get session info
        session = cursor.execute(
            """
            SELECT status, active_agent, created_at, updated_at
            FROM sessions WHERE id = ?
        """,
            (session_id,),
        ).fetchone()

        if not session:
            conn.close()
            return ""

        # Get recent events (excluding text_delta to reduce noise)
        events = cursor.execute(
            """
            SELECT kind, payload, timestamp
            FROM events
            WHERE session_id = ?
            AND kind NOT IN ('text_delta', 'turn_start', 'turn_complete')
            ORDER BY seq DESC
            LIMIT ?
        """,
            (session_id, max_events),
        ).fetchall()

        conn.close()

        if not events:
            return ""

        # Format memory context
        parts = [
            "# Session Memory",
            f"Session ID: {session_id}",
            f"Status: {session[0]}",
            f"Agent: {session[1]}",
            f"Started: {session[2]}",
            "",
            "## Recent Events (most recent first)",
        ]

        for kind, payload_json, timestamp in events:
            try:
                payload = json.loads(payload_json)
                parts.append(f"- [{kind}] @ {timestamp}")

                # Format payload based on event kind
                if kind == "tool_call":
                    tool = payload.get("tool", "unknown")
                    parts.append(f"  Tool: {tool}")
                elif kind == "user_message":
                    content = payload.get("content", "")[:100]  # Truncate long messages
                    parts.append(f"  Message: {content}")
                elif kind == "action":
                    action_type = payload.get("type", "unknown")
                    parts.append(f"  Action: {action_type}")
                else:
                    # Generic payload display (truncated)
                    payload_str = str(payload)[:200]
                    parts.append(f"  Data: {payload_str}")
                parts.append("")
            except Exception:
                logger.debug(
                    "suppressed exception in load_obscura_memory", exc_info=True
                )
                continue  # Skip malformed events

        return "\n".join(parts)

    except Exception as e:
        # Fail gracefully if DB can't be read
        logger.debug("suppressed exception in load_obscura_memory", exc_info=True)
        return f"# Session Memory\nWarning: Could not load session memory ({e})"


def load_session_messages(
    session_id: str, db_path: Path, max_turns: int = 10
) -> list[Message]:
    """Load session history as Message objects for message history reconstruction.

    Args:
        session_id: Session ID to load messages for
        db_path: Path to events.db
        max_turns: Maximum number of conversation turns to load

    Returns:
        List of Message objects (user/assistant pairs) from session history

    """
    if not db_path.exists():
        return []

    def _text_msg(role: Role, text: str) -> Message:
        return Message(role=role, content=[ContentBlock(kind="text", text=text)])

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Get user messages and responses in order
        events = cursor.execute(
            """
            SELECT kind, payload, seq
            FROM events
            WHERE session_id = ?
            AND kind IN ('user_message', 'turn_complete', 'text_delta')
            ORDER BY seq ASC
        """,
            (session_id,),
        ).fetchall()

        conn.close()

        if not events:
            return []

        # Reconstruct conversation turns
        messages: list[Message] = []
        current_assistant_text: list[str] = []

        for kind, payload_json, _seq in events:
            try:
                payload = cast(dict[str, Any], json.loads(payload_json))

                if kind == "user_message":
                    # Flush previous assistant message if any
                    if current_assistant_text:
                        messages.append(
                            _text_msg(Role.ASSISTANT, "".join(current_assistant_text))
                        )
                        current_assistant_text = []

                    # Add user message
                    content = payload.get("content", "")
                    if content:
                        messages.append(_text_msg(Role.USER, str(content)))

                elif kind == "text_delta":
                    # Accumulate assistant response
                    text = payload.get("text", "")
                    if text:
                        current_assistant_text.append(str(text))

                elif kind == "turn_complete":
                    # Finalize assistant message
                    if current_assistant_text:
                        messages.append(
                            _text_msg(Role.ASSISTANT, "".join(current_assistant_text))
                        )
                        current_assistant_text = []

            except Exception:
                logger.debug(
                    "suppressed exception in load_session_messages", exc_info=True
                )
                continue

        # Limit to recent turns
        if len(messages) > max_turns * 2:  # Each turn is user + assistant
            messages = messages[-(max_turns * 2) :]

        return messages

    except Exception as e:
        logger.warning(f"Could not load session messages: {e}")
        return []
