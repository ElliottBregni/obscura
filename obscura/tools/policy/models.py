"""obscura.tools.policy.models — Policy dataclasses for tool access control."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from obscura.core.hooks import HookRegistry
from obscura.core.system_prompts import SUBAGENT_SYSTEM_PROMPT
from obscura.core.types import AgentEventKind

if TYPE_CHECKING:
    from pathlib import Path


def _empty_frozenset() -> frozenset[str]:
    return frozenset()


# ---------------------------------------------------------------------------
# Sub-agent context injection
# ---------------------------------------------------------------------------


def inject_subagent_context(
    agent: Any,
    *,
    tool_allowlist: list[str] | None = None,
) -> None:
    """Inject sub-agent constraints into a child agent before it runs.

    Called by ``make_task_tool`` immediately before ``agent.run_loop()``.

    This function does three things:
    1. Prepends ``SUBAGENT_SYSTEM_PROMPT`` to the agent's system prompt.
    2. Sets ``config.tool_allowlist`` so ``AgentLoop`` enforces it at
       execution time.
    3. If the agent already has a client with a hook registry, installs a
       before-TOOL_CALL hook that rewrites common Claude Code native tool
       names (Glob, Grep, Read, Edit, Write, Bash) to their Obscura
       equivalents (find_files, grep_files, read_text_file, etc.).
    """
    import logging

    _log = logging.getLogger(__name__)

    # --- 1. System prompt ---------------------------------------------------
    if hasattr(agent, "_system_prompt"):
        existing = agent._system_prompt or ""
        agent._system_prompt = (
            SUBAGENT_SYSTEM_PROMPT + "\n\n---\n\n" + existing
            if existing
            else SUBAGENT_SYSTEM_PROMPT
        )

    # --- 2. Tool allowlist --------------------------------------------------
    if tool_allowlist is not None and hasattr(agent, "config"):
        agent.config.tool_allowlist = tool_allowlist
        _log.debug(
            "Set tool_allowlist on agent '%s': %s",
            getattr(agent.config, "name", "?"),
            tool_allowlist,
        )

    # --- 3. Native-tool rewrite hook ----------------------------------------
    _NATIVE_REWRITES: dict[str, str] = {
        "Glob": "find_files",
        "Grep": "grep_files",
        "Read": "read_text_file",
        "Edit": "edit_text_file",
        "Write": "write_text_file",
        "Bash": "run_shell",
    }

    try:
        client = getattr(agent, "_client", None)
        if client is not None:
            hook_reg: HookRegistry | None = getattr(client, "hooks", None)
            if hook_reg is None:
                hook_reg = HookRegistry()
                client.hooks = hook_reg

            def _rewrite_native_tools(event: Any) -> Any:
                """Rewrite Claude Code native tool names to Obscura equivalents."""
                tool_name = getattr(event, "tool_name", None)
                if tool_name and tool_name in _NATIVE_REWRITES:
                    event.tool_name = _NATIVE_REWRITES[tool_name]
                return event

            hook_reg.add_before(_rewrite_native_tools, AgentEventKind.TOOL_CALL)
            _log.debug("Installed native-tool rewrite hook on agent")
    except Exception as exc:
        _log.warning(
            "Could not install native-tool rewrite hook — sub-agent may "
            "fail with NOT_FOUND errors for Glob/Grep/Read/Edit/Write/Bash: %s",
            exc,
        )


# ---------------------------------------------------------------------------
# Policy dataclasses
# ---------------------------------------------------------------------------


def _empty_action_map() -> dict[str, frozenset[str]]:
    return {}


@dataclass(frozen=True)
class ToolPolicy:
    """Declarative policy controlling which tools an agent may invoke.

    Evaluation order:
    1. ``full_access`` -- if True, allow everything.
    2. ``deny_list`` -- if the tool name matches, deny.
    3. ``allow_list`` -- if non-empty, only listed tools are allowed.
    4. ``allowed_actions`` / ``denied_actions`` -- action-level gating.
    5. ``base_dir`` -- if set, file-system tools are restricted to this subtree.
    """

    name: str
    allow_list: frozenset[str] = field(default_factory=_empty_frozenset)
    deny_list: frozenset[str] = field(default_factory=_empty_frozenset)
    base_dir: Path | None = None
    full_access: bool = False
    allowed_actions: dict[str, frozenset[str]] = field(
        default_factory=_empty_action_map,
    )
    denied_actions: dict[str, frozenset[str]] = field(
        default_factory=_empty_action_map,
    )

    @classmethod
    def from_permission_config(
        cls,
        name: str,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        base_dir: Path | None = None,
    ) -> ToolPolicy:
        """Build a :class:`ToolPolicy` from manifest permission lists."""
        return cls(
            name=name,
            allow_list=frozenset(allow) if allow else frozenset(),
            deny_list=frozenset(deny) if deny else frozenset(),
            base_dir=base_dir,
        )


@dataclass(frozen=True)
class PolicyResult:
    """Outcome of evaluating a :class:`ToolPolicy` against a tool invocation."""

    allowed: bool
    reason: str
    matched_rule: str = ""
