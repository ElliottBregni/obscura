"""
obscura.core.system_prompts — Default system prompts for Obscura agents.

Prompts are stored as plain .txt files in obscura/prompts/ and loaded at
runtime. Do not hardcode prompt text in this file.
"""

from __future__ import annotations

from pathlib import Path

# Directory containing all prompt .txt files
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load(name: str) -> str:
    """Load a prompt file by name (without .txt extension)."""
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


# Lazy-loaded module-level constants — preserve the existing public API
# so callers using `from obscura.core.system_prompts import DEFAULT_OBSCURA_SYSTEM_PROMPT`
# continue to work without changes.
def __getattr__(name: str) -> str:
    if name == "DEFAULT_OBSCURA_SYSTEM_PROMPT":
        return _load("default_agent")
    if name == "SUBAGENT_SYSTEM_PROMPT":
        return _load("subagent")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_default_system_prompt() -> str:
    """Return the default Obscura system prompt."""
    return _load("default_agent")


def get_subagent_system_prompt() -> str:
    """Return the sub-agent system prompt."""
    return _load("subagent")


def load_custom_system_prompt(path: Path | str) -> str:
    """Load a custom system prompt from an arbitrary file path."""
    path_obj = Path(path).expanduser()
    if not path_obj.exists():
        raise FileNotFoundError(f"System prompt file not found: {path}")
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


def compose_environment_context(
    *,
    plugin_ids: list[str] | None = None,
    capabilities: list[str] | None = None,
    agent_types: list[str] | None = None,
    bootstrap_summary: str = "",
) -> str:
    """Build an environment context section for the system prompt.

    Loads the ``environment_context.txt`` template and fills it with
    runtime-discovered values (plugins, capabilities, agent types).
    Returns an empty string if the template is missing.
    """
    try:
        template = _load("environment_context")
    except FileNotFoundError:
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
