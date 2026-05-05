"""obscura.core.system_prompts — Default system prompts for Obscura agents.

Prompts are stored as ``.md`` (preferred) or legacy ``.txt`` files in
``obscura/prompts/`` and loaded at runtime. Do not hardcode prompt text in
this file. The loader treats both extensions identically — content is plain
text passed to the model, which interprets markdown formatting natively.
"""

from __future__ import annotations

from pathlib import Path
import logging

logger = logging.getLogger(__name__)


# Directory containing all prompt files (.md preferred, .txt legacy)
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Load a prompt file by name (without extension).

    Tries ``<name>.md`` first, then falls back to ``<name>.txt`` for legacy
    files. Raises ``FileNotFoundError`` if neither exists.

    Public counterpart to the legacy ``_load`` shim — prefer this in new code.
    """
    for ext in (".md", ".txt"):
        path = _PROMPTS_DIR / f"{name}{ext}"
        if path.exists():
            return path.read_text(encoding="utf-8")
    msg = f"Prompt file not found: {_PROMPTS_DIR / name}.(md|txt)"
    raise FileNotFoundError(msg)


# Backwards-compat alias — keep the private name working for older callers.
_load = load_prompt


# Lazy-loaded module-level constants — preserve the existing public API
# so callers using `from obscura.core.system_prompts import DEFAULT_OBSCURA_SYSTEM_PROMPT`
# continue to work without changes.
def __getattr__(name: str) -> str:
    if name == "DEFAULT_OBSCURA_SYSTEM_PROMPT":
        return _load("default_agent")
    if name == "SUBAGENT_SYSTEM_PROMPT":
        return _load("subagent")
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def _resolve_override(filename: str) -> str | None:
    """Look up a prompt override on disk.

    Resolution order (first hit wins):
      1. ``./<filename>`` — project root override (per-repo)
      2. ``~/.obscura/<filename>`` — user-wide override

    Returns the file's stripped contents, or None if neither exists.
    Failures (read errors, permission issues) are logged and treated as
    "no override" so the caller falls back to the built-in template.
    """
    candidates = [
        Path.cwd() / filename,
        Path.home() / ".obscura" / filename,
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning(
                "prompt override exists but couldn't be read: %s",
                candidate,
                exc_info=True,
            )
    return None


def get_default_system_prompt() -> str:
    """Return the default Obscura system prompt.

    Override resolution: ``./sys.md`` → ``~/.obscura/sys.md`` → built-in
    ``obscura/prompts/default_agent.md``. The first override found is used
    verbatim — it replaces the built-in, it does not append to it. Per-agent
    overrides happen one level up via the ``system_prompt`` field in
    ``agents.yaml`` and are layered on top of whatever this returns.
    """
    override = _resolve_override("sys.md")
    if override is not None:
        return override
    return _load("default_agent")


def get_subagent_system_prompt() -> str:
    """Return the sub-agent system prompt.

    Override resolution: ``./subagent.md`` → ``~/.obscura/subagent.md`` →
    built-in ``obscura/prompts/subagent.md``. Same semantics as
    :func:`get_default_system_prompt`.
    """
    override = _resolve_override("subagent.md")
    if override is not None:
        return override
    return _load("subagent")


def load_custom_system_prompt(path: Path | str) -> str:
    """Load a custom system prompt from an arbitrary file path."""
    path_obj = Path(path).expanduser()
    if not path_obj.exists():
        msg = f"System prompt file not found: {path}"
        raise FileNotFoundError(msg)
    return path_obj.read_text(encoding="utf-8")


def compose_system_prompt(
    *,
    base: str = "",
    include_default: bool = True,
    custom_sections: list[str] | None = None,
) -> str:
    """Compose a system prompt from multiple sources.

    Args:
        base: Base system prompt (user-provided)
        include_default: Whether to include default Obscura prompt
        custom_sections: Additional sections to append

    Returns:
        Composed system prompt

    """
    parts: list[str] = []

    if include_default:
        parts.append(get_default_system_prompt())

    if base:
        parts.append(base)

    if custom_sections:
        parts.extend(custom_sections)

    return "\n\n---\n\n".join(parts).strip()


def compose_user_memory_section(*, prefix: str = "user", limit: int = 50) -> str:
    """Build a `## Known about the user` section from `user:*` memories.

    The single memory namespace that's eager-loaded into every system
    prompt. Backend-agnostic: routes through
    :func:`obscura.memory.keyword_store.get_keyword_store` so it works
    identically with SQLite (default) or Postgres
    (``OBSCURA_KEYWORD_MEMORY=postgres``). Empty string if the store
    doesn't exist or has no matching memories. Never blocks boot —
    exceptions are logged at debug and the section is omitted.
    """
    try:
        from obscura.data.keyword_memory import (
            get_keyword_memory_repo,
            keyword_memory_available,
        )
    except ImportError:
        logger.debug("data.keyword_memory unavailable", exc_info=True)
        return ""
    if not keyword_memory_available():
        return ""
    try:
        store = get_keyword_memory_repo()
        try:
            memories = store.list_by_namespace_prefix(prefix, limit=limit)
        finally:
            store.close()
    except Exception:
        logger.debug("user-memory load failed", exc_info=True)
        return ""
    if not memories:
        return ""
    lines: list[str] = ["## Known about the user", ""]
    lines.append(
        "Persistent facts saved across sessions. Update via "
        "`remember_memory(content, namespace='user:...')` whenever the "
        "user reveals a durable preference, goal, or fact about themselves.",
    )
    lines.append("")
    for m in memories:
        lines.append(f"- *(`{m.namespace}`)* {m.content}")
    return "\n".join(lines).strip()


def compose_active_goals_section(*, cwd: str | Path | None = None) -> str:
    """Build a `## Active goals (this project)` section from the GoalBoard.

    Filters to goals whose ``project_root`` equals *cwd* and whose status
    is active/in_progress. Empty string if no goals match. Lazy: only
    runs when called (i.e., once at session start).
    """
    try:
        from obscura.kairos.goals import GoalBoard
    except ImportError:
        logger.debug("kairos.goals unavailable", exc_info=True)
        return ""
    cwd_str = str(Path(cwd).resolve()) if cwd else str(Path.cwd().resolve())
    try:
        board = GoalBoard()
        goals = board.load_all()
    except Exception:
        logger.debug("goal load failed", exc_info=True)
        return ""
    matching = [
        g
        for g in goals
        if g.status in {"active", "in_progress"}
        and g.project_root
        and Path(g.project_root).resolve() == Path(cwd_str)
    ]
    if not matching:
        return ""
    matching.sort(key=lambda g: (g.priority_rank, -g.progress))
    lines: list[str] = ["## Active goals (this project)", ""]
    lines.append(
        "Goals from the goal board (`~/.obscura/goals/`) whose "
        "``project_root`` matches the current working directory. Use "
        "`/goal` slash commands or the goal-board tools to update.",
    )
    lines.append("")
    for g in matching:
        status_tag = f"**{g.status}**" if g.status == "in_progress" else g.status
        progress = f" ({g.progress}%)" if g.progress else ""
        lines.append(f"- {status_tag} `{g.id}` — {g.title}{progress}")
        if g.acceptance_criteria:
            for ac in g.acceptance_criteria[:3]:
                lines.append(f"  - acceptance: {ac}")
    return "\n".join(lines).strip()


def compose_environment_context(
    *,
    plugin_ids: list[str] | None = None,
    capabilities: list[str] | None = None,
    agent_types: list[str] | None = None,
    bootstrap_summary: str = "",
) -> str:
    """Build a runtime-inventory section for the system prompt.

    Loads the ``runtime.md`` template (formerly ``environment_context.txt``)
    and fills it with runtime-discovered values (plugins, capabilities, agent
    types). Returns an empty string if the template is missing.
    """
    try:
        template = load_prompt("runtime")
    except FileNotFoundError:
        # Backwards-compat shim — fall back to the old name for any deploy
        # that hasn't picked up the rename yet.
        try:
            template = load_prompt("environment_context")
        except FileNotFoundError:
            logger.debug(
                "suppressed exception in compose_environment_context",
                exc_info=True,
            )
            return ""

    ids = plugin_ids or []
    caps = capabilities or []
    types = agent_types or []

    plugin_list = "\n".join(f"- {pid}" for pid in ids) if ids else "None discovered"
    capability_list = "\n".join(f"- {c}" for c in caps) if caps else "None configured"
    agent_type_list = ", ".join(types) if types else "loop (default)"

    return template.format(
        plugin_count=len(ids),
        plugin_list=plugin_list,
        capability_list=capability_list,
        agent_types=agent_type_list,
        bootstrap_summary=bootstrap_summary or "All plugins bootstrapped successfully",
    )
