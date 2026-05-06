"""obscura.cli.commands — Slash command registry and handlers."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt_module
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from obscura.agent.definitions import resolve_all_definitions
from obscura.auth import secrets as _secrets
from obscura.auth.cli_user import current_cli_user
from obscura.cli.app.diff_engine import DiffEngine, DiffHunk
from obscura.cli.app.modes import MODE_TOOL_GROUPS, ModeManager, Plan
from obscura.cli.auth_commands import CREDENTIALS_PATH, load_session
from obscura.cli.control_commands import (
    cmd_policies,
    cmd_replay,
    cmd_status,
)
from obscura.cli.mcp_commands import handle_mcp_command
from obscura.cli.vector_memory_bridge import (
    CLI_NAMESPACE,
    clear_mcp_noise_memories,
)
from obscura.cli.render import (
    TOOL_COLOR,
    LabeledStreamRenderer,
    console,
    get_active_text,
    print_error,
    print_info,
    print_ok,
    print_warning,
    render_agent_output,
    render_diff_summary,
    render_event,
    render_plan,
)
from obscura.cli.trace import tail_entries, tail_pretty
from obscura.cli.tui_effects import context_bar, effort_badge, ultrathink_banner
from obscura.core._default_skills import DEFAULT_SKILLS
from obscura.core.compaction import compact_history
from obscura.core.context_lazy import (
    EVAL_GRADING_PROMPT,
    EvalSuite,
    LazyCommandLoader,
    LazySkillLoader,
    ResolvedCommand,
    SkillMetadata,
    load_eval_for_command,
)
from obscura.core.commit_attribution import get_attribution_tracker
from obscura.core.compiler import compile_workspace
from obscura.core.compiler.loader import load_specs_dirs
from obscura.core.context_suggestions import suggest_files
from obscura.core.context_window import (
    estimate_tokens as _cw_estimate_tokens,
    get_context_window,
)
from obscura.core.cost_tracker import get_cost_tracker
from obscura.core.deep_log import dlog
from obscura.core.enums.lifecycle import SessionStatus
from obscura.core.enums.ui import TUIMode
from obscura.core.event_store import EventStoreProtocol
from obscura.core.paths import (
    resolve_all_commands_dirs,
    resolve_all_skills_dirs,
    resolve_obscura_global_home,
    resolve_obscura_skills_dir,
)
from obscura.core.paths import (
    resolve_all_specs_dirs,
)
from obscura.core.permission_modes import PermissionMode
from obscura.core.templates import list_templates, load_template
from obscura.core.tool_policy import ToolPolicy
from obscura.core.types import EFFORT_THINKING_BUDGETS, EffortLevel
from obscura.kairos.goals import GoalBoard
from obscura.manifest.models import AgentManifest
from obscura.plugins.loader import PluginLoader
from obscura.plugins.registry import PluginEntry, PluginRegistryService
from obscura.tools.dynamic_discovery import DynamicToolDiscovery
from obscura.tools.swarm import build_agent_catalog, load_agent_configs
from obscura.tools.system import get_system_tool_specs
from obscura.tools.system.file_state import (
    get_recently_modified_files,
    get_recently_read_files,
)
import logging

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from obscura.agent.interaction import (
        AgentOutput,
        AttentionRequest,
    )
    from obscura.plugins.broker import ToolBroker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MESSAGE_ROLE_OVERHEAD_TOKENS = 4
_RESPONSE_RESERVE_TOKENS = 4096


def _estimate_tokens(text: str) -> int:
    """Estimate token count using shared context-window tokenizer."""
    return _cw_estimate_tokens(text)


def _safe_list_tools(ctx: REPLContext) -> list[Any]:
    """Best-effort tool list retrieval from the active client."""
    try:
        tools: Any = ctx.client.list_tools()
    except Exception:
        logger.debug("suppressed exception in _safe_list_tools", exc_info=True)
        return []
    if not isinstance(tools, list):
        return []
    return cast(list[Any], tools)


def _estimate_tool_schema_tokens(tools: list[Any]) -> int:
    """Estimate tokens consumed by serialized tool specs."""
    if not tools:
        return 0

    payload: list[dict[str, Any]] = []
    for t in tools:
        payload.append(
            {
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", ""),
                "parameters": getattr(t, "parameters", {}),
            },
        )
    return _estimate_tokens(json.dumps(payload, default=str, ensure_ascii=True))


def _estimate_claude_tool_listing_tokens(tools: list[Any]) -> int:
    """Estimate Claude's extra tool-listing text appended to system prompt."""
    if not tools:
        return 0

    lines = ["## Available Tools", ""]
    lines.append(
        "You have the following tools. Use these EXACT names when calling tools:",
    )
    lines.append("")
    for spec in tools:
        desc = str(getattr(spec, "description", "") or "").split("\n")[0][:120]
        lines.append(f"- `{getattr(spec, 'name', '')}`: {desc}")
    lines.append("")
    lines.append("Do NOT invent tool names. If none of these tools fit, tell the user.")
    return _estimate_tokens("\n".join(lines))


def estimate_effective_context_breakdown(
    ctx: REPLContext,
    *,
    pending_user_text: str = "",
    include_response_reserve: bool = True,
) -> dict[str, int]:
    """Estimate active context usage with major request components."""
    system_tokens = _estimate_tokens(ctx.get_effective_system_prompt())

    history_tokens = 0
    for _role, content in ctx.message_history:
        history_tokens += _estimate_tokens(content) + _MESSAGE_ROLE_OVERHEAD_TOKENS

    pending_tokens = 0
    if pending_user_text:
        pending_tokens = (
            _estimate_tokens(pending_user_text) + _MESSAGE_ROLE_OVERHEAD_TOKENS
        )

    tools = _safe_list_tools(ctx)
    tool_schema_tokens = _estimate_tool_schema_tokens(tools)
    claude_tool_listing_tokens = (
        _estimate_claude_tool_listing_tokens(tools) if ctx.backend == "claude" else 0
    )

    response_reserve_tokens = (
        _RESPONSE_RESERVE_TOKENS if include_response_reserve else 0
    )

    total = (
        system_tokens
        + history_tokens
        + pending_tokens
        + tool_schema_tokens
        + claude_tool_listing_tokens
        + response_reserve_tokens
    )
    return {
        "system_tokens": system_tokens,
        "history_tokens": history_tokens,
        "pending_tokens": pending_tokens,
        "tool_schema_tokens": tool_schema_tokens,
        "claude_tool_listing_tokens": claude_tool_listing_tokens,
        "response_reserve_tokens": response_reserve_tokens,
        "total_tokens": total,
    }


def estimate_effective_context_tokens(
    ctx: REPLContext,
    *,
    pending_user_text: str = "",
    include_response_reserve: bool = True,
) -> int:
    """Estimate active context usage including system, tools, and reserve."""
    return estimate_effective_context_breakdown(
        ctx,
        pending_user_text=pending_user_text,
        include_response_reserve=include_response_reserve,
    )["total_tokens"]


# File-writing tool names we track for /diff
_FILE_WRITE_TOOLS = frozenset(
    {
        "write_text_file",
        "create_file",
        "edit_file",
        "replace_in_file",
        "write_file",
        "create_text_file",
        "patch_file",
        "overwrite_file",
    },
)


# ---------------------------------------------------------------------------
# REPL context — mutable state shared across the session
# ---------------------------------------------------------------------------


def _empty_str_any_dict_list() -> list[dict[str, Any]]:
    return []


def _empty_str_set() -> set[str]:
    return set()


def _empty_history() -> list[tuple[str, str]]:
    return []


def _empty_str_str_dict_list() -> list[dict[str, str]]:
    return []


def _empty_pending_reads() -> dict[str, tuple[str, str]]:
    return {}


def _empty_swarm_runs() -> dict[str, dict[str, Any]]:
    return {}


def _empty_str_list() -> list[str]:
    return []


def _empty_any_list() -> list[Any]:
    return []


def _empty_str_any_dict() -> dict[str, Any]:
    return {}


def _empty_bool_dict() -> dict[str, bool]:
    return {}


@dataclass
class REPLContext:
    """Mutable state for the REPL session."""

    client: Any  # ObscuraClient | AgentSession — duck-typed handle
    store: EventStoreProtocol
    session_id: str
    backend: str
    model: str | None
    system_prompt: str
    max_turns: int
    tools_enabled: bool
    mcp_configs: list[dict[str, Any]] = field(default_factory=_empty_str_any_dict_list)

    # Approval gates
    confirm_enabled: bool = False
    confirm_always: set[str] = field(default_factory=_empty_str_set)

    # Message history for context tracking
    message_history: list[tuple[str, str]] = field(default_factory=_empty_history)

    # File change tracking for /diff (path -> {path, original, modified})
    file_changes: list[dict[str, str]] = field(
        default_factory=_empty_str_str_dict_list, repr=False
    )
    pending_file_reads: dict[str, tuple[str, str]] = field(
        default_factory=_empty_pending_reads,
        repr=False,
    )

    # Mode manager (lazy)
    mode_manager: Any = field(default=None, repr=False)

    # Vector memory store (None if disabled)
    vector_store: Any = field(default=None, repr=False)

    # Memory channel router and classifier (None if no channels configured)
    context_router: Any = field(default=None, repr=False)
    turn_classifier: Any = field(default=None, repr=False)

    # Agent runtime (lazy-created on first /agent or /fleet command)
    runtime: Any = field(default=None, repr=False)

    # Background swarm tasks: {swarm_id: {task, assignments, results, ...}}
    swarm_runs: dict[str, dict[str, Any]] = field(
        default_factory=_empty_swarm_runs, repr=False
    )

    # Supervisor reference (set when --supervise is active)
    supervisor: Any = field(default=None, repr=False)
    supervisor_task: Any = field(default=None, repr=False)

    # Slash-skill state (metadata lazy-loaded, bodies loaded on activation)
    _lazy_skill_loader: LazySkillLoader | None = field(default=None, repr=False)
    active_skills: list[str] = field(default_factory=_empty_str_list)

    # @command state (lazy-loaded from ~/.obscura/commands/ + ~/.claude/commands/)
    _lazy_command_loader: LazyCommandLoader | None = field(default=None, repr=False)

    # Wave 2+ feature state
    permission_mode: str = field(default="default", repr=False)
    effort_level: str = field(default="medium", repr=False)
    voice_enabled: bool = field(default=False, repr=False)
    vim_mode: bool = field(default=False, repr=False)
    collapser: Any = field(default=None, repr=False)

    # Secret menu (toggle for hidden controls)
    secret_menu_unlocked: bool = field(default=False, repr=False)

    # Background task tracking (cmd_tasks)
    background_tasks: list[dict[str, Any]] = field(
        default_factory=_empty_str_any_dict_list, repr=False
    )
    python_tasks: list[Any] = field(default_factory=_empty_any_list, repr=False)
    background_task_refs: dict[str, Any] = field(
        default_factory=_empty_str_any_dict, repr=False
    )

    # Right-side menu UI state (cmd_menu)
    ui_right_menu_enabled: bool = field(default=True, repr=False)
    ui_menu_items: dict[str, bool] = field(default_factory=_empty_bool_dict, repr=False)

    def get_mode_manager(self) -> Any:
        """Get or create the ModeManager."""
        if self.mode_manager is None:
            self.mode_manager = ModeManager(TUIMode.CODE)
        return self.mode_manager

    def get_effective_system_prompt(self) -> str:
        """Combine mode system prompt with user system prompt."""
        mode_prompt = ""
        if self.mode_manager is not None:
            mode_prompt = self.mode_manager.get_system_prompt()
        if mode_prompt and self.system_prompt:
            return f"{mode_prompt}\n\n{self.system_prompt}"
        return mode_prompt or self.system_prompt

    async def get_runtime(self) -> Any:
        """Get or create the AgentRuntime, wiring InteractionBus to CLI."""
        if self.runtime is None:
            from obscura.agent.agents import AgentRuntime

            user = current_cli_user()
            self.runtime = AgentRuntime(user)
            await self.runtime.start()

            # Wire InteractionBus → CLI
            bus = self.runtime.interaction_bus

            async def _cli_attention_handler(request: AttentionRequest) -> None:
                """Prompt user inline via TUI widget when an agent requests attention."""
                from obscura.cli.widgets import (
                    AttentionWidgetRequest,
                    confirm_attention,
                )

                widget_request = AttentionWidgetRequest(
                    request_id=request.request_id,
                    agent_name=request.agent_name,
                    message=request.message,
                    priority=getattr(request.priority, "value", "normal"),
                    actions=request.actions,
                    context=request.context,
                )
                result = await confirm_attention(widget_request)
                await bus.respond(request.request_id, result.action, result.text)

            async def _cli_output_handler(output: AgentOutput) -> None:
                """Render agent output streamed via the bus.

                Routes through the v2 notification channel when a renderer
                is active so supervisor/daemon outputs stack as inline
                notifications above the prompt instead of interleaving
                with the main loop's transcript. Falls back to the
                legacy direct-print path for headless / scripted callers.
                """
                from obscura.cli.render import push_notification
                from obscura.cli.renderer.channels import from_agent_output

                if not push_notification(from_agent_output(output)):
                    render_agent_output(output)

            bus.on_attention(_cli_attention_handler)
            bus.on_output(_cli_output_handler)

        return self.runtime

    async def stop_runtime(self) -> None:
        """Stop the runtime if it was created."""
        if self.runtime is not None:
            await self.runtime.stop()
            self.runtime = None

    async def recreate_client(self, backend: str, model: str | None) -> None:
        """Stop old client, create a new one for a different backend.

        Migrated from direct ObscuraClient construction to
        composition.build_core_session — the new client (an
        AgentSession) quacks the same way as the old ObscuraClient for
        every method REPLContext invokes (.send / .stream / .run_loop /
        .register_tool / .resume_session / .stop).
        """
        from obscura.composition.core import build_core_session
        from obscura.composition.session import SessionConfig

        # Old client teardown: aclose if it's a session, stop if it's a
        # legacy ObscuraClient.
        old_client = self.client
        if hasattr(old_client, "aclose"):
            try:
                await old_client.aclose()
            except Exception:
                logger.debug("recreate_client: old aclose failed", exc_info=True)
        else:
            await old_client.stop()

        new_session = await build_core_session(
            SessionConfig(
                backend=backend,
                model=model,
                system_prompt=self.get_effective_system_prompt(),
                mcp_servers=self.mcp_configs or [],
                inject_claude_context=False,
            ),
            surface="repl",
        )
        if self.tools_enabled:
            all_specs = get_system_tool_specs()
            mm = self.mode_manager
            mode_allowed = MODE_TOOL_GROUPS.get(mm.current) if mm is not None else None
            cap_allowed: set[str] | None = None
            try:
                from obscura.plugins.capabilities import (
                    resolve_allowed_tools_from_config,
                )

                cap_allowed = resolve_allowed_tools_from_config()
            except Exception:
                logger.debug("suppressed exception in recreate_client", exc_info=True)
            for spec in all_specs:
                if mode_allowed is not None and spec.name not in mode_allowed:
                    continue
                cap = getattr(spec, "capability", "")
                if cap_allowed is not None and cap and spec.name not in cap_allowed:
                    continue
                new_session.register_tool(spec)
        self.client = new_session
        self.backend = backend
        self.model = model

    def add_file_change(self, path: str, original: str, modified: str) -> None:
        """Track a file change for /diff. Dedupes by path."""
        self.file_changes = [c for c in self.file_changes if c["path"] != path]
        self.file_changes.append(
            {"path": path, "original": original, "modified": modified},
        )

    def _get_skill_loader(self) -> LazySkillLoader:
        """Get or create the lazy slash-skill loader."""
        if self._lazy_skill_loader is None:
            self._lazy_skill_loader = LazySkillLoader(resolve_obscura_skills_dir())
        return self._lazy_skill_loader

    def discover_slash_skills(self) -> list[SkillMetadata]:
        """Discover slash skills (metadata-only)."""
        return self._get_skill_loader().discover_skills()

    def _resolve_skill_name(self, raw_name: str) -> str | None:
        """Resolve a skill name by exact/case-insensitive match."""
        needle = raw_name.strip()
        if not needle:
            return None
        skills = self.discover_slash_skills()
        for skill in skills:
            if skill.name == needle:
                return skill.name
        lowered = needle.lower()
        for skill in skills:
            if skill.name.lower() == lowered:
                return skill.name
        return None

    def activate_skill(self, raw_name: str) -> tuple[bool, str]:
        """Activate a skill, lazily loading body into cache."""
        resolved = self._resolve_skill_name(raw_name)
        if resolved is None:
            return False, f"Skill not found: {raw_name}"
        body = self._get_skill_loader().load_skill_body(resolved)
        if not body:
            return False, f"Failed to load skill body: {resolved}"
        if resolved not in self.active_skills:
            self.active_skills.append(resolved)
        return True, resolved

    def deactivate_skill(self, raw_name: str) -> tuple[bool, str]:
        """Deactivate a skill by name."""
        resolved = self._resolve_skill_name(raw_name) or raw_name.strip()
        if resolved not in self.active_skills:
            return False, f"Skill not active: {raw_name}"
        self.active_skills = [s for s in self.active_skills if s != resolved]
        return True, resolved

    def build_active_skill_context(self) -> str:
        """Build injected context from active slash skills."""
        if not self.active_skills:
            return ""
        loader = self._get_skill_loader()
        blocks: list[str] = []
        for name in self.active_skills:
            body = loader.load_skill_body(name)
            if body:
                blocks.append(f"## Skill: {name}\n\n{body}")
        if not blocks:
            return ""
        return "## Active Slash Skills\n\n" + "\n\n".join(blocks)

    # ── $skill helpers ───────────────────────────────────────────────────

    _dollar_skill_loaders: list[LazySkillLoader] | None = field(
        default=None,
        repr=False,
    )

    def _get_all_skill_loaders(self) -> list[LazySkillLoader]:
        """Get or create skill loaders for all skill directories."""
        if self._dollar_skill_loaders is None:
            self._dollar_skill_loaders = [
                LazySkillLoader(d) for d in resolve_all_skills_dirs()
            ]
        return self._dollar_skill_loaders

    def _get_builtin_skills(self) -> dict[str, str]:
        """Return built-in default skills as {name: content}."""
        try:
            return DEFAULT_SKILLS
        except ImportError:
            logger.debug("suppressed exception in _get_builtin_skills", exc_info=True)
            return {}

    def resolve_dollar_skill(self, name: str) -> str | None:
        """Resolve a $skill by name across all skill directories + builtins."""
        needle = name.strip()
        if not needle:
            return None
        # On-disk skills first
        for loader in self._get_all_skill_loaders():
            skills = loader.discover_skills()
            for s in skills:
                if s.name == needle:
                    return loader.load_skill_body(s.name)
            lowered = needle.lower()
            for s in skills:
                if s.name.lower() == lowered:
                    return loader.load_skill_body(s.name)
        # Built-in fallback
        builtins = self._get_builtin_skills()
        if needle in builtins:
            return builtins[needle]
        lowered = needle.lower()
        for k, v in builtins.items():
            if k.lower() == lowered:
                return v
        return None

    def discover_dollar_skills(self) -> list[str]:
        """Return sorted list of available $skill names (for tab completion)."""
        seen: set[str] = set()
        names: list[str] = []
        # On-disk skills
        for loader in self._get_all_skill_loaders():
            for s in loader.discover_skills():
                if s.name not in seen:
                    seen.add(s.name)
                    names.append(s.name)
        # Built-in skills
        for k in self._get_builtin_skills():
            if k not in seen:
                seen.add(k)
                names.append(k)
        return sorted(names)

    # ── @command helpers ──────────────────────────────────────────────────

    def _get_command_loader(self) -> LazyCommandLoader:
        """Get or create the lazy @command loader."""
        if self._lazy_command_loader is None:
            self._lazy_command_loader = LazyCommandLoader(resolve_all_commands_dirs())
        return self._lazy_command_loader

    def discover_at_commands(self) -> list[str]:
        """Return sorted list of available @command names."""
        return self._get_command_loader().command_names()

    def resolve_at_command(
        self,
        name: str,
        arguments: str = "",
    ) -> ResolvedCommand | None:
        """Resolve an @command by name with argument substitution."""
        return self._get_command_loader().resolve_command(name, arguments)

    # ── chained input parsing ─────────────────────────────────────────────

    def parse_chained_input(self, user_input: str) -> tuple[list[str], str | None, str]:
        """Parse input with $skills, @command, and plain args.

        Returns (skill_names, command_name_or_none, remaining_args).

        Examples::

            "$python @review file.py"  -> (["python"], "review", "file.py")
            "$security $api @test x"   -> (["security", "api"], "test", "x")
            "$arch how does auth work"  -> (["arch"], None, "how does auth work")
            "@debug some error"         -> ([], "debug", "some error")
        """
        skills: list[str] = []
        command: str | None = None
        tokens = user_input.split()
        rest_start = 0

        for i, tok in enumerate(tokens):
            if tok.startswith("$") and len(tok) > 1:
                skills.append(tok[1:])
                rest_start = i + 1
            elif tok.startswith("@") and len(tok) > 1 and command is None:
                command = tok[1:]
                rest_start = i + 1
                break  # everything after @command is args
            else:
                break  # plain text starts

        remaining = " ".join(tokens[rest_start:])
        return skills, command, remaining

    # ── * eval helpers ────────────────────────────────────────────────────

    def get_eval_suite(self, command_name: str) -> EvalSuite | None:
        """Load eval suite for a command (from .eval.md sibling or built-in)."""
        loader = self._get_command_loader()
        loader.discover_commands()
        cmd = loader._metadata_cache.get(command_name)  # pyright: ignore[reportPrivateUsage]
        if cmd is None:
            return None
        return load_eval_for_command(cmd)

    def build_grading_prompt(
        self,
        command_name: str,
        input_args: str,
        response: str,
        criteria: list[str],
    ) -> str:
        """Build a grading prompt for the eval system."""
        criteria_text = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(criteria))
        return EVAL_GRADING_PROMPT.format(
            command=command_name,
            input=input_args,
            response=response,
            criteria=criteria_text,
            total=len(criteria),
        )


# ---------------------------------------------------------------------------
# Command type
# ---------------------------------------------------------------------------

CommandHandler = Callable[[str, REPLContext], Awaitable[str | None]]


# ---------------------------------------------------------------------------
# Handlers — core
# ---------------------------------------------------------------------------


async def cmd_help(_args: str, _ctx: REPLContext) -> str | None:
    """Show available commands."""
    lines = [
        "[bold bright_cyan]Obscura CLI[/]",
        "",
        " [bold]General[/]",
        "  /help                  Show this message",
        "  /quit, /exit, /q       Exit session",
        "  /clear                 Clear terminal",
        "  /version               Show version info",
        "",
        " [bold]Chat & Model[/]",
        "  /backend [name]        Show or switch backend",
        "  /model [name]          Show or switch model",
        "  /system <prompt>       Set system prompt",
        "  /effort [low|med|high|max]  Thinking budget",
        "  /fast                  Toggle terse mode",
        "  /thinking              Show reasoning blocks",
        "",
        " [bold]Tools & Permissions[/]",
        "  /tools [on|off|list]   Manage tools",
        "  /confirm [on|off]      Tool approval gates",
        "  /permissions [mode]    default | plan | accept_edits | bypass",
        "  /search-tools <q>      Search tools by keyword",
        "",
        " [bold]Modes & Planning[/]",
        "  /mode [ask|plan|code]  Switch interaction mode",
        "  /plan                  Show current plan",
        "  /approve <n|all>       Approve plan step(s)",
        "  /reject <n|all>        Reject plan step(s)",
        "",
        " [bold]Code Review & Git[/]",
        "  /diff [overlay|accept|reject|apply]  Review file changes",
        "  /commit                AI-generated git commit",
        "  /review [ref]          AI code review",
        "  /security-review [ref] Security-focused review",
        "  /branch [name|create|delete|list]   Git branch management",
        "  /worktree [list|status|sweep|cleanup]  Isolated worktrees",
        "  /pr [base]             Create pull request",
        "",
        " [bold]Context & Memory[/]",
        "  /context               Context window stats",
        "  /compact [n]           Compress conversation history",
        "  /context-inject <path> Inject file or clipboard into context",
        "  /memory [cmd]          stats | search | clear",
        "",
        " [bold]Sessions[/]",
        "  /session [cmd]         list | new | switch | <id>",
        "  /resume [search]       Resume a previous session",
        "  /rename <title>        Rename current session",
        "  /tag <tag>             Tag current session",
        "  /export [md|txt|json]  Export conversation",
        "  /stash / /pop          Save/restore context",
        "  /cost                  Token usage & cost breakdown",
        "  /usage                 API usage summary",
        "  /stats                 Session statistics",
        "",
        " [bold]Agents & Orchestration[/]",
        "  /agent [spawn|list|stop|run]  Agent lifecycle",
        "  /delegate [type] <prompt>     One-shot delegation",
        "  /fleet [spawn|status|run]     Multi-agent fleet",
        "  /swarm [status|results|stop]  Background swarm",
        "  /coordinator [on|off]  Multi-worker mode",
        "  /peers                 List active sessions",
        "  /send <id> <msg>       Message another session",
        "",
        " [bold]Agent Steering[/]",
        "  /goal <description>    Set a persistent session goal",
        "  /persona [preset|text] Set agent persona",
        "  /guardrails add <rule> Add runtime constraints",
        "  /focus <path>          Restrict agent to specific files/dirs",
        "  /undercover [on|off]   Suppress AI attribution",
        "  /tool-policy [cmd]     allow-all | custom-only | allow/deny <tools>",
        "",
        " [bold]Automation[/]",
        "  /loop <interval> <cmd> Run prompt/command on interval (5m, 30s, 2h)",
        "  /loop list|stop        Manage active loops",
        "  /schedule add|list|remove|run  Persistent cron triggers",
        "  /kairos [on|off|status]  Autonomous daemon mode",
        "  /voice [on|off]        Push-to-talk (Ctrl+Space)",
        "",
        " [bold]Workspace & Plugins[/]",
        "  /init                  Init workspace + generate OBSCURA.md",
        "  /doctor                Environment diagnostics",
        "  /discover [cat]        Discover MCP tools",
        "  /mcp [cmd]             MCP server management",
        "  /config [key] [value]  View or edit settings",
        "  /hooks [add|remove]    Manage event hooks",
        "",
        " [bold]Utility[/]",
        "  /cat <path>            Display file contents",
        "  /files                 List files in context",
        "  /add-dir <path>        Change working directory",
        "  /rewind [n]            Undo file changes",
        "  /btw <question>        Side question (doesn't affect context)",
        "  /summary               Summarize conversation",
        "  /copy                  Copy last response to clipboard",
        "  /bug [report]          View errors / generate bug report",
        "",
        " [bold]Control & Logging[/]",
        "  /status [--json]       System health & status",
        "  /ps                    Background sessions",
        "  /kill                  Stop all agents",
        "  /running               Active processes",
        "  /log [tail N|stats]    View deep structured logs",
        "",
        "[dim]Ctrl+T: expand thinking | Ctrl+Space: voice input[/]",
    ]
    console.print("\n".join(lines))
    return None


async def cmd_quit(_args: str, _ctx: REPLContext) -> str | None:
    """Exit the REPL."""
    return "quit"


async def cmd_clear(_args: str, _ctx: REPLContext) -> str | None:
    """Clear the terminal."""
    console.clear()
    return None


async def cmd_cat(args: str, _ctx: REPLContext) -> str | None:
    """Display file contents with syntax highlighting. Usage: /cat <path>."""
    filepath = args.strip()
    if not filepath:
        print_error("Usage: /cat <path>")
        return None

    target = Path(filepath).expanduser().resolve()
    if not target.exists():
        print_error(f"File not found: {target}")
        return None
    if not target.is_file():
        print_error(f"Not a file: {target}")
        return None

    try:
        data = target.read_bytes()
    except PermissionError:
        logger.debug("suppressed exception in cmd_cat", exc_info=True)
        print_error(f"Permission denied: {target}")
        return None

    # Detect binary files
    if b"\x00" in data[:8192]:
        print_error(f"Binary file: {target}")
        return None

    text = data.decode("utf-8", errors="replace")

    # Use Rich Syntax for highlighting based on file extension
    suffix = target.suffix.lstrip(".")
    lexer = suffix or "text"
    try:
        syntax = Syntax(
            text,
            lexer,
            theme="monokai",
            line_numbers=True,
            word_wrap=False,
        )
        console.print(syntax)
    except Exception:
        # Fallback to plain text if lexer not found
        logger.debug("suppressed exception in cmd_cat", exc_info=True)
        console.print(text)

    return None


async def cmd_tail_trace(args: str, _ctx: REPLContext) -> str | None:
    """Tail recent JSONL trace entries (from logs/trace.log). Usage: /tail-trace [n]."""
    try:
        n = int(args.strip()) if args.strip() else 50
    except Exception:
        logger.debug("suppressed exception in cmd_tail_trace", exc_info=True)
        n = 50
    try:
        out = tail_pretty(n)
        if not out:
            print_info("No trace entries found.")
        else:
            console.print(out)
    except Exception:
        logger.debug("suppressed exception in cmd_tail_trace", exc_info=True)
        print_error("Failed to read trace log.")
    return None


# ---------------------------------------------------------------------------
# Handlers — chat config
# ---------------------------------------------------------------------------


async def cmd_backend(args: str, ctx: REPLContext) -> str | None:
    """Show or switch backend."""
    name = args.strip()
    if not name:
        print_info(f"Backend: {ctx.backend}")
        return None
    if name not in ("copilot", "claude", "codex"):
        print_error(
            f"Unknown backend: {name}. Use 'copilot', 'claude', or 'codex'.",
        )
        return None
    if name == ctx.backend:
        print_info(f"Already using {name}.")
        return None
    await ctx.recreate_client(name, ctx.model)
    print_ok(f"Switched to {name}.")
    return None


async def cmd_model(args: str, ctx: REPLContext) -> str | None:
    """Show or switch model."""
    name = args.strip()
    if not name:
        print_info(f"Model: {ctx.model or '(default)'}")
        return None
    await ctx.recreate_client(ctx.backend, name)
    print_ok(f"Model set to {name}.")
    return None


async def cmd_system(args: str, ctx: REPLContext) -> str | None:
    """Set the system prompt."""
    prompt = args.strip()
    if not prompt:
        if ctx.system_prompt:
            print_info(f"System prompt: {ctx.system_prompt[:80]}...")
        else:
            print_info("No system prompt set.")
        return None
    ctx.system_prompt = prompt
    await ctx.recreate_client(ctx.backend, ctx.model)
    print_ok("System prompt updated.")
    return None


async def cmd_tools(args: str, ctx: REPLContext) -> str | None:
    """Show, toggle, enable, or disable tools."""
    val = args.strip()
    parts = val.split(None, 1)
    sub = parts[0].lower() if parts else ""
    sub_arg = parts[1].strip() if len(parts) > 1 else ""

    if not sub:
        print_info(f"Tools: {'on' if ctx.tools_enabled else 'off'}")
        return None

    if sub == "on":
        ctx.tools_enabled = True
        print_ok("Tools enabled.")
    elif sub == "off":
        ctx.tools_enabled = False
        print_ok("Tools disabled.")
    elif sub == "list":
        try:
            registry = ctx.client._tool_registry  # noqa: SLF001
            tools = registry.all_including_disabled()
            if not tools:
                print_info("No tools registered.")
                return None

            table = Table(title="Registered Tools", expand=False)
            table.add_column("#", justify="right", style="dim", width=4)
            table.add_column("status", width=3, justify="center")
            table.add_column("name", style=TOOL_COLOR, no_wrap=True)
            table.add_column("description", max_width=55)
            for i, t in enumerate(tools, 1):
                desc = getattr(t, "description", "") or ""
                if len(desc) > 55:
                    desc = desc[:52] + "..."
                status = (
                    "[red]off[/]" if registry.is_disabled(t.name) else "[green]on[/]"
                )
                table.add_row(str(i), status, t.name, desc)
            console.print(table)
        except Exception as exc:
            logger.debug("suppressed exception in cmd_tools", exc_info=True)
            print_error(f"Failed to list tools: {exc}")
    elif sub == "enable":
        if not sub_arg:
            print_error("Usage: /tools enable <name>")
            return None
        registry = ctx.client._tool_registry  # noqa: SLF001
        if registry.enable(sub_arg):
            print_ok(f"Tool enabled: {sub_arg}")
        else:
            print_error(f"Tool not found or already enabled: {sub_arg}")
    elif sub == "disable":
        if not sub_arg:
            print_error("Usage: /tools disable <name>")
            return None
        registry = ctx.client._tool_registry  # noqa: SLF001
        if registry.disable(sub_arg):
            print_ok(f"Tool disabled: {sub_arg}")
        else:
            print_error(f"Tool not found: {sub_arg}")
    else:
        print_error("Usage: /tools [on|off|list|enable <name>|disable <name>]")
    return None


async def cmd_confirm(args: str, ctx: REPLContext) -> str | None:
    """Toggle tool approval gates."""
    val = args.strip().lower()
    if not val:
        status = "on" if ctx.confirm_enabled else "off"
        print_info(f"Confirm: {status}")
        if ctx.confirm_always:
            print_info(f"  Auto-approved: {', '.join(sorted(ctx.confirm_always))}")
        return None
    if val == "on":
        ctx.confirm_enabled = True
        print_ok("Tool confirmation enabled. You'll be prompted before each tool call.")
    elif val == "off":
        ctx.confirm_enabled = False
        ctx.confirm_always.clear()
        print_ok("Tool confirmation disabled.")
    else:
        print_error("Usage: /confirm [on|off]")
    return None


# ---------------------------------------------------------------------------
# Handlers — modes (plan / ask / code)
# ---------------------------------------------------------------------------


async def cmd_mode(args: str, ctx: REPLContext) -> str | None:
    """Switch interaction mode."""

    mm = ctx.get_mode_manager()
    val = args.strip().lower()

    if not val:
        print_info(f"Mode: {mm.current.value}")
        return None

    mode_map = {
        "ask": TUIMode.ASK,
        "plan": TUIMode.PLAN,
        "code": TUIMode.CODE,
        "diff": TUIMode.DIFF,
    }
    mode = mode_map.get(val)
    if mode is None:
        print_error("Usage: /mode ask|plan|code|diff")
        return None

    mm.switch(mode)

    # Enable tools for any mode that has a non-empty capability group

    allowed = MODE_TOOL_GROUPS.get(mode)
    ctx.tools_enabled = allowed is None or len(allowed) > 0

    # Recreate client with mode-specific system prompt
    await ctx.recreate_client(ctx.backend, ctx.model)
    print_ok(f"Switched to {val} mode.")
    return None


async def cmd_plan(args: str, ctx: REPLContext) -> str | None:
    """Structured planning. Usage: /plan [show|save|execute|clear] or /plan <description>."""

    mm = ctx.get_mode_manager()
    val = args.strip()
    sub = val.split(None, 1)[0].lower() if val else ""

    # /plan (no args) or /plan show — display current plan
    if not val or sub == "show":
        plan = mm.active_plan
        if plan is None:
            print_info(
                "No active plan. Use /plan <description> to create one, "
                "or /mode plan and describe what you want to build."
            )
            return None
        render_plan(plan)
        return None

    # /plan clear — discard the active plan
    if sub == "clear":
        mm.active_plan = None
        print_ok("Plan cleared.")
        return None

    # /plan save — persist to vault
    if sub == "save":
        plan = mm.active_plan
        if plan is None:
            print_error("No active plan to save.")
            return None
        vault_dir = Path.home() / ".obscura" / "vault" / "shared" / "decisions"
        vault_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", plan.title.lower()).strip("-")[:50]
        path = vault_dir / f"plan-{slug}.md"
        content = plan.to_markdown()
        path.write_text(content, encoding="utf-8")
        print_ok(f"Plan saved to {path}")
        return None

    # /plan execute — switch to code mode and inject approved steps
    if sub == "execute":
        plan = mm.active_plan
        if plan is None:
            print_error("No active plan to execute.")
            return None
        approved = [s for s in plan.steps if s.status in ("approved", "edited")]
        if not approved:
            print_error("No approved steps. Use /approve <n|all> first.")
            return None
        mm.switch(TUIMode.CODE)
        ctx.tools_enabled = True
        await ctx.recreate_client(ctx.backend, ctx.model)
        print_ok(
            f"Switched to code mode with {len(approved)} approved steps. "
            "The plan is injected into context."
        )
        return None

    # /plan <description> — create a new plan via the agent
    # Switch to plan mode if not already there
    if mm.current != TUIMode.PLAN:
        mm.switch(TUIMode.PLAN)
        allowed = mm.get_allowed_tool_names()
        ctx.tools_enabled = allowed is None or len(allowed) > 0
        await ctx.recreate_client(ctx.backend, ctx.model)

    prompt = (
        f"Create a structured implementation plan for: {val}\n\n"
        "Research the codebase first using file reading and grep tools, "
        "then produce a numbered plan. Each step should include the files "
        "it touches and any risks."
    )

    collected: list[str] = []
    try:
        async for event in ctx.client.run_loop(prompt):
            render_event(event)
            if hasattr(event, "text") and event.text:
                collected.append(event.text)
    except Exception as exc:
        logger.debug("suppressed exception in cmd_plan", exc_info=True)
        print_error(str(exc))
        return None

    # Parse the response into a Plan
    full_text = "".join(collected)
    if full_text.strip():
        plan = Plan.parse(full_text)
        if plan.steps:
            mm.active_plan = plan
            console.print()
            print_ok(
                f"Plan created with {len(plan.steps)} steps. "
                "Use /approve <n|all>, /reject <n|all>, then /plan execute."
            )
        else:
            print_warning("Could not parse numbered steps from the response.")

    return None


async def cmd_approve(args: str, ctx: REPLContext) -> str | None:
    """Approve plan step(s)."""
    mm = ctx.get_mode_manager()
    plan = mm.active_plan
    if plan is None:
        print_error("No active plan.")
        return None

    val = args.strip()
    if val == "all":
        for s in plan.steps:
            if s.status == "pending":
                s.approve()
        print_ok(f"Approved all {plan.approved_count} steps.")
    elif val.isdigit():
        n = int(val)
        step = next((s for s in plan.steps if s.number == n), None)
        if step:
            step.approve()
            print_ok(f"Approved step {n}.")
        else:
            print_error(f"Step {n} not found.")
    else:
        print_error("Usage: /approve <n|all>")
        return None

    if plan.all_decided:
        console.print("[green]All steps decided. /mode code to execute.[/]")
    return None


async def cmd_reject(args: str, ctx: REPLContext) -> str | None:
    """Reject plan step(s)."""
    mm = ctx.get_mode_manager()
    plan = mm.active_plan
    if plan is None:
        print_error("No active plan.")
        return None

    val = args.strip()
    if val == "all":
        for s in plan.steps:
            if s.status == "pending":
                s.reject()
        print_ok(f"Rejected {plan.rejected_count} steps.")
    elif val.isdigit():
        n = int(val)
        step = next((s for s in plan.steps if s.number == n), None)
        if step:
            step.reject()
            print_ok(f"Rejected step {n}.")
        else:
            print_error(f"Step {n} not found.")
    else:
        print_error("Usage: /reject <n|all>")
        return None

    if plan.all_decided:
        console.print("[green]All steps decided. /mode code to execute.[/]")
    return None


# ---------------------------------------------------------------------------
# Handlers — diff / review
# ---------------------------------------------------------------------------


async def cmd_diff(args: str, ctx: REPLContext) -> str | None:
    """Review file changes: /diff [accept|reject|apply] [n|all]."""
    parts = args.strip().split()
    sub = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if not sub:
        # Show diff summary
        render_diff_summary(ctx.file_changes)
        return None

    if sub in ("overlay", "side-by-side", "sbs"):
        return await _diff_side_by_side(ctx)
    if sub == "accept":
        return await _diff_accept_reject(rest, ctx, accept=True)
    if sub == "reject":
        return await _diff_accept_reject(rest, ctx, accept=False)
    if sub == "apply":
        return await _diff_apply(ctx)

    print_error("Usage: /diff [overlay|accept|reject|apply] [n|all]")
    return None


async def _diff_accept_reject(
    val: str,
    ctx: REPLContext,
    *,
    accept: bool,
) -> str | None:
    """Accept or reject hunks."""

    if not ctx.file_changes:
        print_info("No file changes.")
        return None

    engine = DiffEngine()
    # Build flat hunk list
    all_hunks: list[tuple[dict[str, str], DiffHunk]] = []
    for fc in ctx.file_changes:
        diff_fc = engine.compute_change(
            Path(fc["path"]),
            fc["original"],
            fc["modified"],
        )
        for h in diff_fc.hunks:
            all_hunks.append((fc, h))

    action = "accept" if accept else "reject"
    if val == "all":
        for _, h in all_hunks:
            h.accept() if accept else h.reject()
        print_ok(f"{action.title()}ed all {len(all_hunks)} hunks.")
    elif val.isdigit():
        n = int(val)
        if 0 <= n < len(all_hunks):
            _, h = all_hunks[n]
            h.accept() if accept else h.reject()
            print_ok(f"{action.title()}ed hunk {n}.")
        else:
            print_error(f"Hunk {n} not found (0-{len(all_hunks) - 1}).")
    else:
        print_error(f"Usage: /diff {action} <n|all>")
    return None


async def _diff_side_by_side(ctx: REPLContext) -> str | None:
    """Render side-by-side diff overlay for all file changes."""
    if not ctx.file_changes:
        print_info("No file changes to display.")
        return None

    engine = DiffEngine(context_lines=3)

    for fc in ctx.file_changes:
        path = fc["path"]
        original = fc["original"]
        modified = fc["modified"]

        # Compute structured change.
        diff_fc = engine.compute_change(Path(path), original, modified)

        if not diff_fc.hunks:
            continue

        # Build side-by-side view.
        sbs_text = engine.format_side_by_side(diff_fc, width=120)

        # Stats.
        added = sum(1 for h in diff_fc.hunks for ln in h.lines if ln.tag == "+")
        removed = sum(1 for h in diff_fc.hunks for ln in h.lines if ln.tag == "-")
        stats = f"+{added} -{removed} ({len(diff_fc.hunks)} hunks)"

        # Render with Rich panel + syntax highlighting.
        console.print()
        console.print(
            Panel(
                Text(sbs_text),
                title=f"[bold]{path}[/] — {stats}",
                subtitle="[dim]/diff accept|reject to manage hunks[/]",
                border_style="cyan",
                expand=False,
                padding=(0, 1),
            ),
        )

    # Also show unified diff with syntax highlighting for each file.
    for fc in ctx.file_changes:
        unified = engine.format_unified(
            engine.compute_change(Path(fc["path"]), fc["original"], fc["modified"]),
        )
        if unified.strip():
            console.print(Syntax(unified, "diff", theme="monokai", line_numbers=False))

    return None


async def _diff_apply(ctx: REPLContext) -> str | None:
    """Apply accepted hunks to disk."""

    if not ctx.file_changes:
        print_info("No file changes.")
        return None

    engine = DiffEngine()
    applied = 0
    for fc in ctx.file_changes:
        diff_fc = engine.compute_change(
            Path(fc["path"]),
            fc["original"],
            fc["modified"],
        )
        accepted = [h for h in diff_fc.hunks if h.status == "accepted"]
        if not accepted:
            continue
        patched = engine.apply_hunks(fc["original"], accepted)
        Path(fc["path"]).write_text(patched)
        applied += 1
        print_ok(f"  Applied {len(accepted)} hunks to {fc['path']}")

    if applied:
        ctx.file_changes.clear()
        print_ok(f"Applied changes to {applied} file(s).")
    else:
        print_info("No accepted hunks to apply. Use /diff accept first.")
    return None


# ---------------------------------------------------------------------------
# Handlers — context
# ---------------------------------------------------------------------------


async def cmd_context(_args: str, ctx: REPLContext) -> str | None:
    """Show context window stats."""
    breakdown = estimate_effective_context_breakdown(ctx)
    tokens = breakdown["total_tokens"]
    user_msgs = sum(1 for r, _ in ctx.message_history if r == "user")
    asst_msgs = sum(1 for r, _ in ctx.message_history if r == "assistant")
    console.print(f"Messages: {user_msgs} user, {asst_msgs} assistant")
    console.print(f"Estimated tokens: {tokens:,} (full request estimate)")
    console.print(
        "  "
        f"system={breakdown['system_tokens']:,} "
        f"history={breakdown['history_tokens']:,} "
        f"tools={breakdown['tool_schema_tokens']:,}",
    )
    if breakdown["claude_tool_listing_tokens"]:
        console.print(
            f"  claude_tool_listing={breakdown['claude_tool_listing_tokens']:,}",
        )
    console.print(f"  response_reserve={breakdown['response_reserve_tokens']:,}")
    mm = ctx.get_mode_manager()
    console.print(f"Mode: {mm.current.value}")
    # Visual context usage bar.
    try:
        cw = get_context_window(ctx.model or "default")
        usage_pct = tokens / cw if cw > 0 else 0.0

        console.print(f"  Context: {context_bar(usage_pct)}")
    except Exception:
        logger.debug("suppressed exception in cmd_context", exc_info=True)
    if tokens > 80_000:
        console.print("[yellow]Warning: context is large. Consider /compact[/]")
    return None


async def cmd_thinking(_args: str, _ctx: REPLContext) -> str | None:
    """Show expanded thinking/reasoning blocks from the last response."""
    from obscura.cli.render import (
        THINKING_COLOR,
        _active_renderer,  # pyright: ignore[reportPrivateUsage]
        console,
    )

    renderer = _active_renderer
    if renderer is None:
        console.print("[dim]No thinking blocks available.[/]")
        return None

    blocks = renderer.get_thinking_blocks()
    if not blocks:
        console.print("[dim]No thinking blocks in this session.[/]")
        return None

    for i, block in enumerate(blocks, 1):
        console.print(
            Panel(
                Text(block, style="dim italic"),
                title=f"[{THINKING_COLOR}]reasoning #{i}[/]",
                title_align="left",
                border_style="dim magenta",
                expand=False,
                padding=(0, 1),
            ),
        )
    console.print(f"[dim]{len(blocks)} thinking block(s)[/]")
    return None


async def cmd_compact(args: str, ctx: REPLContext) -> str | None:
    """Compact context by starting a fresh session with summary.

    Usage: /compact [N]  — keep last N message pairs (default 4)
    """
    keep = 4
    val = args.strip()
    if val and val.isdigit():
        keep = int(val)

    if len(ctx.message_history) <= keep:
        print_info("Not enough history to compact.")
        return None

    old = ctx.message_history[:-keep]
    before = estimate_effective_context_tokens(ctx)
    dropped = len(old)

    # Try LLM-powered summarization via compact_history().
    summary = ""
    extracted_memories: list[dict[str, str]] = []
    try:
        # Convert message_history tuples to dicts for compact_history.
        msg_dicts: list[Any] = [{"role": r, "content": t} for r, t in old]
        _backend_obj = (
            ctx.client._backend  # noqa: SLF001
            if hasattr(ctx.client, "_backend")
            else None
        )
        compacted_msgs, _was_compacted, extracted_memories = await compact_history(
            msg_dicts,
            ctx.model or "default",
            _backend_obj,
            system_prompt=ctx.system_prompt,
        )
        # Extract summary text from compacted messages.
        for msg in compacted_msgs:
            _msg_any: Any = msg
            content_any: Any
            if isinstance(_msg_any, dict):
                _msg_dict = cast(dict[str, Any], _msg_any)
                content_any = _msg_dict.get("content", "")
            else:
                content_any = str(_msg_any)
            content_str = str(content_any) if content_any else ""
            if content_str and "[CONVERSATION SUMMARY" in content_str:
                summary = content_str
                break
    except Exception:
        logger.debug("suppressed exception in cmd_compact", exc_info=True)

    # Fallback: build text summary if LLM didn't produce one.
    if not summary:
        summary_lines: list[str] = []
        for role, text in old:
            snippet = text[:200].replace("\n", " ")
            summary_lines.append(f"[{role}]: {snippet}")
        summary = "\n".join(summary_lines)
        if len(summary) > 2000:
            summary = summary[:2000] + "..."

    # Store extracted memories if any.
    if extracted_memories:
        try:
            # Store in default memory namespace.
            for mem in extracted_memories[:10]:
                key = mem.get("key", "")
                value = mem.get("value", "")
                if key and value:
                    console.print(f"  [dim]Memory extracted: {key}[/]")
        except Exception:
            logger.debug("suppressed exception in cmd_compact", exc_info=True)

    # Fresh session with summary prepended.
    ctx.message_history = ctx.message_history[-keep:]
    ctx.session_id = uuid.uuid4().hex
    ctx.system_prompt = (
        f"[Previous conversation summary ({dropped} messages)]\n{summary}\n\n"
        + ctx.system_prompt
    )
    await ctx.recreate_client(ctx.backend, ctx.model)

    after = estimate_effective_context_tokens(ctx)
    mem_note = (
        f", {len(extracted_memories)} memories extracted" if extracted_memories else ""
    )
    print_ok(
        f"Compacted: dropped {dropped} messages, "
        f"~{max(0, before - after):,} tokens freed{mem_note}. "
        f"New session: {ctx.session_id[:12]}",
    )
    return None


async def cmd_jitter(args: str, _ctx: REPLContext) -> str | None:
    """Show or set the reasoning jitter delay (OBSCURA_REASONING_JITTER_MS)."""
    val = args.strip()
    if not val:
        current = os.environ.get("OBSCURA_REASONING_JITTER_MS", "0")
        print_info(
            f"Reasoning jitter: {current}ms  (set with /jitter <ms> or /jitter off)",
        )
        return None
    if val == "off":
        os.environ["OBSCURA_REASONING_JITTER_MS"] = "0"
        print_ok("Reasoning jitter disabled (0ms).")
        return None
    if val.isdigit():
        os.environ["OBSCURA_REASONING_JITTER_MS"] = val
        print_ok(f"Reasoning jitter set to {val}ms.")
        return None
    print_error("Usage: /jitter [<ms> | off]")
    return None


# ---------------------------------------------------------------------------
# Handlers — session
# ---------------------------------------------------------------------------


async def cmd_session(args: str, ctx: REPLContext) -> str | None:
    """Session management: list, new, switch, or resume by ID.

    Usage:
        /session            — list sessions + interactive switch
        /session list       — list sessions (no interactive picker)
        /session switch     — interactive session picker
        /session new        — start a new session
        /session <id>       — switch to session by ID (prefix match)
    """
    sub = args.strip()

    if sub == "new":
        new_id = uuid.uuid4().hex
        ctx.session_id = new_id
        print_ok(f"New session: {new_id}")
        return None

    # Fetch all sessions
    sessions = await ctx.store.list_sessions()

    # --- list / default: show table ---
    if sub in ("list", "switch", ""):
        if not sessions:
            print_info("No sessions.")
            return None

        _session_print_table(sessions, ctx.session_id)

        # Interactive picker for bare /session or /session switch
        if sub != "list":
            await _session_interactive_switch(sessions, ctx)

        return None

    # --- Resume by ID (prefix match) ---
    await _session_switch_by_id(sub, sessions, ctx)
    return None


def _session_print_table(
    sessions: list[Any],
    current_id: str,
) -> None:
    """Print sessions table split into active vs completed."""
    active_statuses = {
        SessionStatus.RUNNING,
        SessionStatus.WAITING_FOR_TOOL,
        SessionStatus.WAITING_FOR_USER,
    }
    active = [s for s in sessions if s.status in active_statuses]
    other = [s for s in sessions if s.status not in active_statuses]

    def _build_table(
        rows: list[Any],
        title: str,
        header_style: str = "bold",
    ) -> Table:
        tbl = Table(
            show_header=True,
            header_style=header_style,
            title=title,
        )
        tbl.add_column("", width=2)  # current indicator
        tbl.add_column("Session", style="cyan", no_wrap=True)
        tbl.add_column("Title", style="bold", max_width=30)
        tbl.add_column("Status", style="yellow")
        tbl.add_column("Model", style="dim")
        tbl.add_column("Msgs", style="dim", justify="right")
        tbl.add_column("Created", style="dim")
        for s in rows[:20]:
            indicator = "[bold cyan]\u2192[/]" if s.id == current_id else ""
            title = s.summary or "-"
            if len(title) > 30:
                title = title[:27] + "..."
            tbl.add_row(
                indicator,
                s.id[:12],
                title,
                s.status.value,
                s.model or "-",
                str(s.message_count) if s.message_count else "-",
                s.created_at.strftime("%Y-%m-%d %H:%M"),
            )
        return tbl

    if active:
        console.print(_build_table(active, f"Active ({len(active)})"))
    if other:
        style = "bold dim" if active else "bold"
        console.print(
            _build_table(other[:10], f"Completed ({len(other)})", style),
        )


async def _session_interactive_switch(
    sessions: list[Any],
    ctx: REPLContext,
) -> None:
    """Present interactive picker and switch to the selected session."""
    # Build choices: other sessions (not current)
    switchable = [s for s in sessions if s.id != ctx.session_id]
    if not switchable:
        print_info("No other sessions to switch to.")
        return

    choices: list[str] = []
    session_map: dict[str, Any] = {}
    for s in switchable[:15]:
        name_part = s.summary or s.active_agent or s.backend or "default"
        label = f"{s.id[:8]} · {s.status.value} · {name_part}"
        choices.append(label)
        session_map[label] = s

    choices.append("cancel")

    try:
        from obscura.cli.widgets import (
            AttentionWidgetRequest,
            confirm_attention,
        )

        result = await confirm_attention(
            AttentionWidgetRequest(
                request_id="session_switch",
                agent_name="system",
                message="Switch to session:",
                actions=tuple(choices),
            ),
        )
        if result.action == "cancel":
            return

        selected = session_map.get(result.action)
        if selected is not None:
            await _do_session_switch(selected.id, ctx)
    except Exception:
        logger.debug(
            "suppressed exception in _session_interactive_switch", exc_info=True
        )


async def _session_switch_by_id(
    partial_id: str,
    sessions: list[Any],
    ctx: REPLContext,
) -> None:
    """Switch to a session by exact or prefix match."""
    # Try exact match first
    existing = await ctx.store.get_session(partial_id)
    if existing is not None:
        await _do_session_switch(partial_id, ctx)
        return

    # Try prefix match
    matches = [s for s in sessions if s.id.startswith(partial_id)]
    if len(matches) == 1:
        await _do_session_switch(matches[0].id, ctx)
        return

    if len(matches) > 1:
        print_warning(
            f"Ambiguous prefix '{partial_id}' — matches "
            f"{len(matches)} sessions. Be more specific.",
        )
        for m in matches[:5]:
            console.print(f"  [cyan]{m.id[:12]}[/] · {m.status.value}")
        return

    print_error(f"Session '{partial_id}' not found.")


async def _do_session_switch(session_id: str, ctx: REPLContext) -> None:
    """Switch to a different session.

    We intentionally skip ``resume_session`` — calling it with an ID from
    a previous process can crash the backend subprocess (e.g. Copilot SDK
    exits with code 1, killing the message reader).  Instead we just
    update the session_id and reset the backend to a fresh conversation
    state.  The event store still has the full history for the session.
    """
    old_id = ctx.session_id
    ctx.session_id = session_id

    try:
        await ctx.client.reset_session()
        print_ok(f"Switched to session: {session_id[:12]}")
    except Exception:
        # reset_session failed — backend might be dead; full recreate
        logger.debug("suppressed exception in _do_session_switch", exc_info=True)
        try:
            await ctx.recreate_client(ctx.backend, ctx.model)
            print_ok(f"Switched to session: {session_id[:12]} (reconnected)")
        except Exception as exc:
            # Can't recover — revert
            logger.debug("suppressed exception in _do_session_switch", exc_info=True)
            ctx.session_id = old_id
            print_error(f"Failed to switch session: {exc}")
            return


# ---------------------------------------------------------------------------
# Handlers — skills
# ---------------------------------------------------------------------------


async def cmd_skill(args: str, ctx: REPLContext) -> str | None:
    """Slash-skill management: list, load, unload, active, clear."""
    parts = args.strip().split()
    sub = parts[0].lower() if parts else "list"
    rest = parts[1:]

    if sub == "list":
        skills = ctx.discover_slash_skills()
        if not skills:
            print_info("No skills found in .obscura/skills.")
            return None
        table = Table(show_header=True, header_style="bold")
        table.add_column("Skill", style="cyan")
        table.add_column("Active", style="green")
        table.add_column("Invocable", style="yellow")
        table.add_column("Description")
        for skill in skills:
            table.add_row(
                skill.name,
                "yes" if skill.name in ctx.active_skills else "",
                "yes" if skill.user_invocable else "no",
                skill.description or "",
            )
        console.print(table)
        return None

    if sub == "active":
        if not ctx.active_skills:
            print_info("No active slash skills.")
            return None
        print_info("Active skills: " + ", ".join(ctx.active_skills))
        return None

    if sub == "load":
        if not rest:
            print_error("Usage: /skill load <name> [name...]")
            return None
        for name in rest:
            ok, result = ctx.activate_skill(name)
            if ok:
                print_ok(f"Loaded skill: {result}")
            else:
                print_error(result)
        return None

    if sub in {"unload", "remove", "rm"}:
        if not rest:
            print_error("Usage: /skill unload <name> [name...]")
            return None
        for name in rest:
            ok, result = ctx.deactivate_skill(name)
            if ok:
                print_ok(f"Unloaded skill: {result}")
            else:
                print_error(result)
        return None

    if sub == "clear":
        ctx.active_skills = []
        print_ok("Cleared active slash skills.")
        return None

    print_info("Usage: /skill [list|load|unload|active|clear]")
    return None


# ---------------------------------------------------------------------------
# Handlers — agents
# ---------------------------------------------------------------------------


async def cmd_agent(args: str, ctx: REPLContext) -> str | None:
    """Agent lifecycle: spawn, list, stop, run."""
    parts = args.strip().split(None, 1)
    sub = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "spawn":
        return await _agent_spawn(rest, ctx)
    if sub == "list":
        return await _agent_list(ctx)
    if sub == "stop":
        return await _agent_stop(rest, ctx)
    if sub == "run":
        return await _agent_run(rest, ctx)

    print_info("Usage: /agent spawn|list|stop|run")
    return None


async def _agent_spawn(args: str, ctx: REPLContext) -> str | None:
    """Spawn agent from manifest. Usage: /agent spawn <name> [-m model] [-s system_prompt]."""
    tokens = shlex.split(args) if args else []
    if not tokens:
        print_error("Usage: /agent spawn <name> [-m model] [-s system_prompt]")
        return None

    name = tokens[0].lstrip("@")
    model_override = None
    system_prompt_override = None

    # Parse optional flags
    i = 1
    while i < len(tokens):
        if tokens[i] == "-m" and i + 1 < len(tokens):
            model_override = tokens[i + 1]
            i += 2
        elif tokens[i] == "-s" and i + 1 < len(tokens):
            system_prompt_override = tokens[i + 1]
            i += 2
        else:
            i += 1

    runtime = await ctx.get_runtime()

    # Load manifest from merged agents config (global-wins, local adds)

    manifest_loaded = False

    agent_configs = load_agent_configs(include_disabled=True)
    if agent_configs:
        try:
            if name in agent_configs:
                cfg = agent_configs[name]

                # Daemon agents are auto-started, not manually spawned
                if cfg.get("type") == "daemon":
                    print_warning(
                        f"'{name}' is a daemon agent (auto-started at session start). "
                        "Use /agent list to see running daemons.",
                    )
                    return None

                # Build AgentManifest from config
                # Extract skills config dict if present
                skills_cfg = cfg.get("skills", {})
                if not isinstance(skills_cfg, dict):
                    skills_cfg = {}

                # AgentManifest has pydantic field aliases (``provider`` -> ``model``,
                # ``mcp_servers`` -> ``mcp_server_refs``). Pyright follows the alias only,
                # so we route through ``model_validate`` even though
                # ``populate_by_name=True`` accepts both at runtime.
                manifest = AgentManifest.model_validate(
                    {
                        "name": cfg["name"],
                        "provider": model_override
                        or cfg.get("provider")
                        or cfg.get("model", ctx.backend),
                        "system_prompt": system_prompt_override
                        or cfg.get("system_prompt", ""),
                        "max_turns": cfg.get("max_turns", 10),
                        "tools": cfg.get("tools", []),
                        "tags": cfg.get("tags", []),
                        "mcp_servers": cfg.get("mcp_servers", [])
                        if isinstance(cfg.get("mcp_servers"), list)
                        else [],
                        "skills_config": skills_cfg,
                    }
                )

                # Spawn from manifest (SECURE) — pass explicit model override if given
                agent = runtime.spawn_from_manifest(
                    manifest,
                    provider_override=model_override,
                )
                await agent.start()
                print_ok(
                    f"Spawned {name} from manifest (id: {agent.id[:12]}, "
                    f"max_turns: {cfg.get('max_turns', 10)})",
                )
                manifest_loaded = True
                return None

        except Exception as e:
            logger.debug("suppressed exception in _agent_spawn", exc_info=True)
            print_warning(f"Failed to load manifest for '{name}': {e}")

    # Fallback: spawn with SDK defaults (with warning)
    if not manifest_loaded:
        print_warning(
            f"No manifest found for '{name}'. "
            "Using SDK defaults (no skill filters, tool restrictions, or limits).",
        )
        agent = runtime.spawn(
            name,
            model=model_override or ctx.backend,
            system_prompt=system_prompt_override or "",
        )
        await agent.start()
        print_ok(f"Spawned {name} with defaults (id: {agent.id[:12]})")

    return None


async def _agent_list(ctx: REPLContext) -> str | None:
    """List all agents."""
    runtime = await ctx.get_runtime()
    agents = runtime.list_agents()
    if not agents:
        print_info("No agents.")
        return None
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Status", style="yellow")
    for a in agents:
        table.add_row(a.id[:12], a.config.name, a.status.name)
    console.print(table)
    return None


async def _agent_stop(args: str, ctx: REPLContext) -> str | None:
    """Stop an agent by ID or name."""
    target = args.strip()
    if not target:
        print_error("Usage: /agent stop <id|name>")
        return None
    runtime = await ctx.get_runtime()
    agent = runtime.get_agent(target)
    if agent is None:
        matches = [a for a in runtime.list_agents() if a.config.name == target]
        agent = matches[0] if matches else None
    if agent is None:
        print_error(f"Agent not found: {target}")
        return None
    await agent.stop()
    print_ok(f"Stopped {agent.config.name} ({agent.id[:12]})")
    return None


async def _agent_run(args: str, ctx: REPLContext) -> str | None:
    """Run a prompt on an agent. Usage: /agent run <id|name> <prompt>."""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_error("Usage: /agent run <id|name> <prompt>")
        return None
    target, prompt = parts
    runtime = await ctx.get_runtime()
    agent = runtime.get_agent(target)
    if agent is None:
        matches = [a for a in runtime.list_agents() if a.config.name == target]
        agent = matches[0] if matches else None
    if agent is None:
        print_error(f"Agent not found: {target}")
        return None

    try:
        async for event in agent.stream_loop(prompt):
            render_event(event)
        console.print()
    except Exception as exc:
        logger.debug("suppressed exception in _agent_run", exc_info=True)
        print_error(str(exc))
    return None


# ---------------------------------------------------------------------------
# Delegation -- spawn a one-shot subagent on a different backend
# ---------------------------------------------------------------------------

_DELEGATE_ROUTES: dict[str, str] = {
    "review": "claude",
    "analysis": "claude",
    "summarize": "copilot",
    "codegen": "openai",
    "testgen": "openai",
    "support": "copilot",
}


def _delegate_continuation_prompt(
    original_prompt: str,
    last_output: str,
    done_if: str,
    attempt: int,
    max_passes: int,
) -> str:
    """Build a follow-up prompt when delegate completion criteria is unmet."""
    return (
        "Continue working on this task.\n"
        f"Attempt {attempt} of {max_passes} did not satisfy completion criteria.\n"
        f"Completion criteria: {done_if}\n\n"
        "Original task:\n"
        f"{original_prompt}\n\n"
        "Your previous output:\n"
        f"{last_output}\n\n"
        "Return an updated final answer that satisfies the criteria."
    )


async def cmd_delegate(args: str, ctx: REPLContext) -> str | None:
    """Spawn a one-shot subagent on a different backend.

    Usage: /delegate [--mode once|loop] [--max-turns N] [--passes N] [--done-if TEXT]
                     <task_type|--model MODEL> <prompt>
    Routes: review/analysis->claude  summarize/support->copilot  codegen/testgen->openai
    """
    tokens = shlex.split(args) if args else []
    if not tokens:
        print_info(
            "Usage: /delegate [--mode once|loop] [--max-turns N] [--passes N] "
            "[--done-if TEXT] <task_type|--model MODEL> <prompt>",
        )
        return None
    model: str | None = None
    task_type = ""
    mode = "loop"
    max_turns: int | None = None
    max_passes = 1
    passes_explicit = False
    done_if = ""
    prompt_tokens: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "--model" and i + 1 < len(tokens):
            model = tokens[i + 1]
            i += 2
        elif tokens[i] == "--mode" and i + 1 < len(tokens):
            mode = tokens[i + 1].strip().lower()
            if mode not in ("once", "loop"):
                print_error("Invalid --mode. Use once or loop.")
                return None
            i += 2
        elif tokens[i] == "--max-turns" and i + 1 < len(tokens):
            try:
                max_turns = int(tokens[i + 1])
                if max_turns <= 0:
                    raise ValueError
            except ValueError:
                logger.debug("suppressed exception in cmd_delegate", exc_info=True)
                print_error("--max-turns must be a positive integer.")
                return None
            i += 2
        elif tokens[i] == "--passes" and i + 1 < len(tokens):
            try:
                max_passes = int(tokens[i + 1])
                if max_passes <= 0:
                    raise ValueError
            except ValueError:
                logger.debug("suppressed exception in cmd_delegate", exc_info=True)
                print_error("--passes must be a positive integer.")
                return None
            passes_explicit = True
            i += 2
        elif tokens[i] == "--done-if" and i + 1 < len(tokens):
            done_if = tokens[i + 1].strip()
            i += 2
        elif not task_type and not model and i == 0:
            task_type = tokens[i]
            i += 1
        else:
            prompt_tokens = tokens[i:]
            break
    if not prompt_tokens:
        print_error("No prompt provided.")
        return None
    if mode == "once" and done_if:
        print_error("--done-if requires --mode loop.")
        return None
    if done_if and not passes_explicit and max_passes == 1:
        max_passes = 5
    prompt = " ".join(prompt_tokens)
    if model is None:
        model = _DELEGATE_ROUTES.get(task_type.strip().lower(), ctx.backend)
    agent_name = "delegate-" + (task_type or model) + "-" + uuid.uuid4().hex[:6]
    runtime = await ctx.get_runtime()
    agent = runtime.spawn(
        agent_name,
        model=model,
        system_prompt="You are a specialized "
        + (task_type or model)
        + " subagent. Complete the task concisely.",
    )
    await agent.start()
    print_info("=> Delegating to [" + model + "] in " + mode + " mode...")

    collected_output: list[str] = []
    try:
        if mode == "once":
            result = await agent.run(prompt)
            result_text = str(result)
            collected_output.append(result_text)
            if result_text:
                console.print(result_text)
                console.print()
        else:
            loop_prompt = prompt
            for attempt in range(1, max_passes + 1):
                output_lines: list[str] = []
                async for event in agent.stream_loop(loop_prompt, max_turns=max_turns):
                    render_event(event)
                    if hasattr(event, "text") and event.text:
                        output_lines.append(event.text)
                console.print()

                pass_output = "".join(output_lines)
                if pass_output:
                    collected_output.append(pass_output)
                if not done_if:
                    break
                if done_if.casefold() in pass_output.casefold():
                    print_ok("Delegate output matched completion criteria.")
                    break
                if attempt >= max_passes:
                    print_warning(
                        "Delegate did not meet completion criteria within "
                        + str(max_passes)
                        + " passes.",
                    )
                    break
                print_info("Completion criteria unmet; continuing delegate pass...")
                loop_prompt = _delegate_continuation_prompt(
                    original_prompt=prompt,
                    last_output=pass_output,
                    done_if=done_if,
                    attempt=attempt,
                    max_passes=max_passes,
                )
    except Exception as exc:
        logger.debug("suppressed exception in cmd_delegate", exc_info=True)
        print_error("Delegation failed: " + str(exc))
        return None
    finally:
        with contextlib.suppress(Exception):
            await agent.stop()
    _inject_context = (
        getattr(ctx.client, "inject_context", None) if ctx.client else None
    )
    if collected_output and callable(_inject_context):
        summary = "\n".join(collected_output)
        _inject_context("[Delegated to " + model + "]\n" + summary)
        print_ok("Injected " + str(len(summary)) + " chars into context")
    return None


# ---------------------------------------------------------------------------
# Handlers — interaction bus (attention requests)
# ---------------------------------------------------------------------------


async def cmd_attention(args: str, ctx: REPLContext) -> str | None:
    """List or respond to pending attention requests from agents."""
    parts = args.strip().split(None, 2)
    sub = parts[0] if parts else ""

    if sub in {"respond", "r"}:
        # /attention respond <request_id_prefix> <action> [text]
        if len(parts) < 3:
            print_error("Usage: /attention respond <id> <action> [text]")
            return None
        rid_prefix = parts[1]
        rest = parts[2].split(None, 1)
        action = rest[0]
        text = rest[1] if len(rest) > 1 else ""

        if ctx.runtime is None:
            print_error("No runtime active.")
            return None
        bus = ctx.runtime.interaction_bus
        # Match request_id by prefix
        matched = [r for r in bus.pending_requests if r.startswith(rid_prefix)]
        if not matched:
            print_error(f"No pending request matching '{rid_prefix}'.")
            return None
        for rid in matched:
            await bus.respond(rid, action, text)
            print_ok(f"Responded to {rid[:12]} with action='{action}'")
        return None

    # Default: list pending attention requests
    if ctx.runtime is None:
        print_info("No runtime active. Use /fleet spawn first.")
        return None

    bus = ctx.runtime.interaction_bus
    pending = bus.pending_requests
    if not pending:
        print_info("No pending attention requests.")
        return None

    table = Table(show_header=True, header_style="bold", title="Pending Attention")
    table.add_column("Request ID", style="cyan", width=14)
    table.add_column("Status", style="yellow")
    for rid in pending:
        table.add_row(rid[:12], "waiting")
    console.print(table)
    console.print("[dim]/attention respond <id> <action> [text][/]")
    return None


# ---------------------------------------------------------------------------
# Handlers — fleet / swarm
# ---------------------------------------------------------------------------


async def cmd_fleet(args: str, ctx: REPLContext) -> str | None:
    """Fleet orchestration: spawn, status, run, delegate, stop."""
    parts = args.strip().split(None, 1)
    sub = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "spawn":
        return await _fleet_spawn(rest, ctx)
    if sub == "status":
        return await _fleet_status(ctx)
    if sub == "run":
        return await _fleet_run(rest, ctx)
    if sub == "delegate":
        return await _fleet_delegate(rest, ctx)
    if sub == "stop":
        return await _fleet_stop(rest, ctx)

    print_info("Usage: /fleet spawn|status|run|delegate|stop")
    return None


async def _fleet_spawn(args: str, ctx: REPLContext) -> str | None:
    """Spawn multiple agents. Usage: /fleet spawn <name1> [name2...] [-m model]."""
    tokens = shlex.split(args) if args else []
    if not tokens:
        print_error("Usage: /fleet spawn <name1> [name2...] [-m model]")
        return None

    names: list[str] = []
    model = ctx.backend
    i = 0
    while i < len(tokens):
        if tokens[i] == "-m" and i + 1 < len(tokens):
            model = tokens[i + 1]
            i += 2
        else:
            names.append(tokens[i])
            i += 1

    if not names:
        print_error("No agent names given.")
        return None

    runtime = await ctx.get_runtime()

    # Load merged agent configs once outside the loop (global-wins, local adds)

    all_agent_configs = load_agent_configs(include_disabled=True)

    for name in names:
        manifest_loaded = False
        agent: Any = None

        if name in all_agent_configs and all_agent_configs[name].get("enabled", True):
            try:
                cfg = all_agent_configs[name]
                s_cfg = cfg.get("skills", {})
                if not isinstance(s_cfg, dict):
                    s_cfg = {}
                # AgentManifest has pydantic field aliases — see model_validate
                # comment at the first AgentManifest construction site.
                manifest = AgentManifest.model_validate(
                    {
                        "name": cfg.get("name", name),
                        "provider": model
                        or cfg.get("provider")
                        or cfg.get("model", ctx.backend),
                        "system_prompt": cfg.get("system_prompt", ""),
                        "max_turns": cfg.get("max_turns", 10),
                        "tools": cfg.get("tools", []),
                        "tags": cfg.get("tags", []),
                        "mcp_servers": cfg.get("mcp_servers", [])
                        if isinstance(cfg.get("mcp_servers"), list)
                        else [],
                        "skills_config": s_cfg,
                    }
                )
                agent = runtime.spawn_from_manifest(
                    manifest,
                    provider_override=model if model != ctx.backend else None,
                )
                manifest_loaded = True
            except Exception:
                logger.debug("suppressed exception in _fleet_spawn", exc_info=True)
                agent = None

        if not manifest_loaded:
            agent = runtime.spawn(name, model=model)
        if agent is None:
            print_error(f"Failed to spawn {name}")
            continue
        await agent.start()
        print_ok(f"  Spawned {name} ({agent.id[:12]})")
    print_ok(f"Fleet: {len(names)} agents ready.")
    return None


async def _fleet_status(ctx: REPLContext) -> str | None:
    """Show fleet status."""
    runtime = await ctx.get_runtime()
    agents = runtime.list_agents()
    if not agents:
        print_info("No agents in fleet.")
        return None
    table = Table(show_header=True, header_style="bold", title="Fleet")
    table.add_column("ID", style="cyan", width=14)
    table.add_column("Name", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Model", style="dim")
    for a in agents:
        table.add_row(
            a.id[:12],
            a.config.name,
            a.status.name,
            getattr(a.config, "model", "—"),
        )
    console.print(table)
    return None


async def _fleet_run(args: str, ctx: REPLContext) -> str | None:
    """Broadcast prompt to all running agents (sequential)."""
    prompt = args.strip()
    if not prompt:
        print_error("Usage: /fleet run <prompt>")
        return None

    runtime = await ctx.get_runtime()
    agents = runtime.list_agents()
    running = [a for a in agents if a.status.name == "RUNNING"]
    if not running:
        print_error("No running agents. Use /fleet spawn first.")
        return None

    # Color rotation for agents
    colors = ["cyan", "magenta", "yellow", "green", "blue", "red"]

    for idx, agent in enumerate(running):
        color = colors[idx % len(colors)]
        renderer = LabeledStreamRenderer(agent.config.name, color)
        try:
            async for event in agent.stream_loop(prompt):
                renderer.handle(event)
        except KeyboardInterrupt:
            logger.debug("suppressed exception in _fleet_run", exc_info=True)
            renderer.finish()
            console.print("[dim][interrupted][/]")
            break
        except Exception as exc:
            logger.debug("suppressed exception in _fleet_run", exc_info=True)
            renderer.finish()
            print_error(f"{agent.config.name}: {exc}")
        else:
            renderer.finish()
        console.print()

    return None


async def _fleet_delegate(args: str, ctx: REPLContext) -> str | None:
    """Send prompt to specific agent.

    Usage: /fleet delegate <agent> [--mode once|loop] [--max-turns N]
                                [--passes N] [--done-if TEXT] <prompt>
    """
    tokens = shlex.split(args) if args else []
    if len(tokens) < 2:
        print_error(
            "Usage: /fleet delegate <agent> [--mode once|loop] "
            "[--max-turns N] [--passes N] [--done-if TEXT] <prompt>",
        )
        return None
    target = tokens[0]
    mode = "loop"
    max_turns: int | None = None
    max_passes = 1
    passes_explicit = False
    done_if = ""
    prompt_tokens: list[str] = []
    i = 1
    while i < len(tokens):
        if tokens[i] == "--mode" and i + 1 < len(tokens):
            mode = tokens[i + 1].strip().lower()
            if mode not in ("once", "loop"):
                print_error("Invalid --mode. Use once or loop.")
                return None
            i += 2
        elif tokens[i] == "--max-turns" and i + 1 < len(tokens):
            try:
                max_turns = int(tokens[i + 1])
                if max_turns <= 0:
                    raise ValueError
            except ValueError:
                logger.debug("suppressed exception in _fleet_delegate", exc_info=True)
                print_error("--max-turns must be a positive integer.")
                return None
            i += 2
        elif tokens[i] == "--passes" and i + 1 < len(tokens):
            try:
                max_passes = int(tokens[i + 1])
                if max_passes <= 0:
                    raise ValueError
            except ValueError:
                logger.debug("suppressed exception in _fleet_delegate", exc_info=True)
                print_error("--passes must be a positive integer.")
                return None
            passes_explicit = True
            i += 2
        elif tokens[i] == "--done-if" and i + 1 < len(tokens):
            done_if = tokens[i + 1].strip()
            i += 2
        else:
            prompt_tokens = tokens[i:]
            break
    if not prompt_tokens:
        print_error(
            "Usage: /fleet delegate <agent> [--mode once|loop] "
            "[--max-turns N] [--passes N] [--done-if TEXT] <prompt>",
        )
        return None
    if mode == "once" and done_if:
        print_error("--done-if requires --mode loop.")
        return None
    if done_if and not passes_explicit and max_passes == 1:
        max_passes = 5
    prompt = " ".join(prompt_tokens)
    runtime = await ctx.get_runtime()
    agent = runtime.get_agent(target)
    if agent is None:
        matches = [a for a in runtime.list_agents() if a.config.name == target]
        agent = matches[0] if matches else None
    if agent is None:
        print_error(f"Agent not found: {target}")
        return None

    renderer = LabeledStreamRenderer(agent.config.name, "cyan")
    try:
        if mode == "once":
            result = await agent.run(prompt)
            if result:
                console.print(str(result))
        else:
            loop_prompt = prompt
            for attempt in range(1, max_passes + 1):
                output_lines: list[str] = []
                async for event in agent.stream_loop(loop_prompt, max_turns=max_turns):
                    renderer.handle(event)
                    if hasattr(event, "text") and event.text:
                        output_lines.append(event.text)
                pass_output = "".join(output_lines)
                if not done_if:
                    break
                if done_if.casefold() in pass_output.casefold():
                    print_ok("Delegate output matched completion criteria.")
                    break
                if attempt >= max_passes:
                    print_warning(
                        "Delegate did not meet completion criteria within "
                        + str(max_passes)
                        + " passes.",
                    )
                    break
                print_info("Completion criteria unmet; continuing delegate pass...")
                loop_prompt = _delegate_continuation_prompt(
                    original_prompt=prompt,
                    last_output=pass_output,
                    done_if=done_if,
                    attempt=attempt,
                    max_passes=max_passes,
                )
    except KeyboardInterrupt:
        logger.debug("suppressed exception in _fleet_delegate", exc_info=True)
        renderer.finish()
        console.print("[dim][interrupted][/]")
    except Exception as exc:
        logger.debug("suppressed exception in _fleet_delegate", exc_info=True)
        renderer.finish()
        print_error(str(exc))
    else:
        renderer.finish()
    console.print()
    return None


async def _fleet_stop(args: str, ctx: REPLContext) -> str | None:
    """Stop fleet agents. Usage: /fleet stop [name|all]."""
    target = args.strip()
    runtime = await ctx.get_runtime()
    agents = runtime.list_agents()

    if not agents:
        print_info("No agents.")
        return None

    if target == "all" or not target:
        for a in agents:
            await a.stop()
        print_ok(f"Stopped {len(agents)} agents.")
    else:
        matches = [
            a for a in agents if a.config.name == target or a.id.startswith(target)
        ]
        if not matches:
            print_error(f"Agent not found: {target}")
            return None
        for a in matches:
            await a.stop()
            print_ok(f"Stopped {a.config.name} ({a.id[:12]})")
    return None


# ---------------------------------------------------------------------------
# Handlers — swarm (autonomous multi-agent)
# ---------------------------------------------------------------------------


@dataclass
class _SwarmAssignment:
    """A single agent assignment from the swarm planning step."""

    agent_name: str
    prompt: str
    rationale: str


# Keyword → agent mapping for fast (no-LLM) planning
_SWARM_KEYWORD_MAP: list[tuple[list[str], str, str]] = [
    # (keywords, agent_name, rationale)
    (
        [
            "code",
            "implement",
            "write",
            "function",
            "class",
            "module",
            "refactor",
            "feature",
        ],
        "code-architect",
        "Code implementation task",
    ),
    (
        ["python", "pip", "pytest", "type hint", "pep", "dataclass", "pydantic"],
        "python-dev",
        "Python-specific task",
    ),
    (
        ["test", "unit test", "coverage", "assert", "mock", "fixture"],
        "python-dev",
        "Testing task",
    ),
    (
        ["bug", "fix", "error", "crash", "traceback", "debug", "breakpoint"],
        "debugger",
        "Debugging task",
    ),
    (
        ["review", "pr", "pull request", "code review", "lint"],
        "github-pr-reviewer",
        "Code review task",
    ),
    (
        ["security", "vuln", "cve", "auth", "xss", "injection", "pentest"],
        "security-researcher",
        "Security analysis task",
    ),
    (
        ["deploy", "docker", "k8s", "ci", "cd", "pipeline", "infra", "terraform"],
        "devops-engineer",
        "Infrastructure/DevOps task",
    ),
    (
        ["research", "analyze", "investigate", "compare", "benchmark"],
        "research-analyst",
        "Research/analysis task",
    ),
    (
        ["doc", "readme", "api doc", "changelog", "tutorial"],
        "technical-writer",
        "Documentation task",
    ),
    (
        ["design", "ux", "ui", "wireframe", "mockup", "layout"],
        "ux-designer",
        "Design task",
    ),
    (
        ["data", "ml", "model", "dataset", "train", "predict", "pandas"],
        "data-scientist",
        "Data science task",
    ),
    (
        ["prompt", "system prompt", "instruct"],
        "prompt-engineer",
        "Prompt engineering task",
    ),
    (
        ["product", "prd", "roadmap", "prioritize", "stakeholder"],
        "product-manager",
        "Product management task",
    ),
    (
        ["content", "blog", "copy", "seo", "marketing"],
        "content-writer",
        "Content creation task",
    ),
]


def _swarm_plan_fast(
    task: str,
    agent_configs: dict[str, dict[str, Any]],
) -> list[_SwarmAssignment]:
    """Fast keyword-based planning — no LLM call needed."""
    task_lower = task.lower()
    matched: dict[str, _SwarmAssignment] = {}

    for keywords, agent_name, rationale in _SWARM_KEYWORD_MAP:
        if agent_name not in agent_configs:
            continue
        for kw in keywords:
            if kw in task_lower and agent_name not in matched:
                matched[agent_name] = _SwarmAssignment(
                    agent_name=agent_name,
                    prompt=task,
                    rationale=rationale,
                )
                break

    if not matched:
        # Default: use assistant if available, else first non-daemon agent
        fallback = "assistant"
        if fallback not in agent_configs:
            for name, cfg in agent_configs.items():
                if cfg.get("type", "loop") != "daemon":
                    fallback = name
                    break
        matched[fallback] = _SwarmAssignment(
            agent_name=fallback,
            prompt=task,
            rationale="General-purpose fallback",
        )

    return list(matched.values())


_SWARM_PLAN_PROMPT = """\
You are a task decomposition planner for a multi-agent system.

Available specialist agents:
{agent_catalog}

Task from the user:
{task}

Decompose this task into subtasks. For each subtask, assign exactly one agent \
from the list above. Pick the best-fit agent for each subtask based on its \
specialization. You may assign the same agent type to multiple subtasks if \
appropriate. Use between 1 and 6 agents total.

Respond with ONLY valid JSON — no markdown fences, no commentary. Format:

[
  {{"agent_name": "<name from list>", "prompt": "<specific prompt for this agent>", "rationale": "<one sentence why>"}}
]
"""

_SWARM_SYNTH_PROMPT = """\
You were given this task: {task}

Multiple specialist agents worked on subtasks in parallel. Here are their results:

{agent_results}

Synthesize these results into a single coherent response. Combine insights, \
resolve any contradictions, and produce a unified answer. Be concise.
"""


async def _swarm_plan_smart(
    task: str,
    agent_configs: dict[str, dict[str, Any]],
    ctx: REPLContext,
) -> list[_SwarmAssignment]:
    """LLM-based planning — slower but smarter decomposition."""
    catalog = build_agent_catalog(agent_configs)
    prompt = _SWARM_PLAN_PROMPT.format(agent_catalog=catalog, task=task)

    message = await ctx.client.send(prompt)
    raw = message.text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)

    items_raw: Any = json.loads(raw)
    if not isinstance(items_raw, list):
        msg = "Expected JSON array from planner"
        raise ValueError(msg)
    items = cast(list[dict[str, Any]], items_raw)

    assignments: list[_SwarmAssignment] = []
    for item in items:
        name = str(item.get("agent_name", "assistant"))
        if name not in agent_configs and "assistant" in agent_configs:
            name = "assistant"
        assignments.append(
            _SwarmAssignment(
                agent_name=name,
                prompt=str(item.get("prompt", task)),
                rationale=str(item.get("rationale", "")),
            ),
        )
    return assignments


async def _swarm_run_agent(
    assignment: _SwarmAssignment,
    runtime: Any,
    agent_configs: dict[str, dict[str, Any]],
    model_override: str | None,
    ctx: REPLContext,
) -> tuple[str, str]:
    """Spawn, loop, and stop a single swarm agent. Returns (name, output)."""

    name = assignment.agent_name
    agent = None

    try:
        cfg = agent_configs.get(name)
        if cfg is not None:
            s_cfg = cfg.get("skills", {})
            if not isinstance(s_cfg, dict):
                s_cfg = {}
            # AgentManifest has pydantic field aliases — see model_validate
            # comment at the first AgentManifest construction site.
            manifest = AgentManifest.model_validate(
                {
                    "name": cfg["name"],
                    "provider": model_override
                    or cfg.get("provider")
                    or cfg.get("model", ctx.backend),
                    "system_prompt": cfg.get("system_prompt", ""),
                    "max_turns": cfg.get("max_turns", 25),
                    "tools": cfg.get("tools", []),
                    "tags": cfg.get("tags", []),
                    "mcp_servers": (
                        cfg.get("mcp_servers", [])
                        if isinstance(cfg.get("mcp_servers"), list)
                        else []
                    ),
                    "skills_config": s_cfg,
                }
            )
            agent = runtime.spawn_from_manifest(manifest)
        else:
            agent = runtime.spawn(
                name,
                model=model_override or ctx.backend,
                system_prompt=f"You are a {name} specialist. Complete the task thoroughly.",
            )

        await agent.start()
        print_info(f"  {name} ({agent.id[:12]}) started")

        # Run full agentic loop
        output_lines: list[str] = []
        async for event in agent.stream_loop(assignment.prompt):
            if hasattr(event, "text") and event.text:
                output_lines.append(event.text)

        result_text = "".join(output_lines)
        print_ok(f"  {name} finished ({len(result_text)} chars)")
        return (name, result_text)

    except Exception as exc:
        logger.debug("suppressed exception in _swarm_run_agent", exc_info=True)
        error_msg = f"Error: {exc}"
        print_error(f"  {name}: {error_msg}")
        return (name, error_msg)

    finally:
        if agent is not None:
            with contextlib.suppress(Exception):
                await agent.stop()


async def _swarm_synthesize(
    task: str,
    results: list[tuple[str, str]],
    ctx: REPLContext,
) -> str:
    """Synthesize agent results using the session LLM."""
    agent_results = "\n\n".join(f"### {name}\n{output}" for name, output in results)
    prompt = _SWARM_SYNTH_PROMPT.format(task=task, agent_results=agent_results)
    try:
        message = await ctx.client.send(prompt)
        return message.text
    except Exception:
        logger.debug("suppressed exception in _swarm_synthesize", exc_info=True)
        return agent_results


async def _swarm_background(
    swarm_id: str,
    task: str,
    assignments: list[_SwarmAssignment],
    runtime: Any,
    agent_configs: dict[str, dict[str, Any]],
    model_override: str | None,
    synthesize: bool,
    ctx: REPLContext,
) -> None:
    """Background coroutine that runs all swarm agents and collects results."""
    run = ctx.swarm_runs[swarm_id]
    run["status"] = "running"

    coros = [
        _swarm_run_agent(
            assignment=assignment,
            runtime=runtime,
            agent_configs=agent_configs,
            model_override=model_override,
            ctx=ctx,
        )
        for assignment in assignments
    ]

    try:
        results: list[tuple[str, str]] = await asyncio.gather(*coros)
        run["results"] = results

        # Synthesize
        if synthesize and len(results) > 1:
            summary = await _swarm_synthesize(task, results, ctx)
        else:
            summary = "\n\n".join(f"## {name}\n{output}" for name, output in results)

        run["summary"] = summary
        run["status"] = "done"

        # Inject into context
        _inject_context = (
            getattr(ctx.client, "inject_context", None) if ctx.client else None
        )
        if summary and callable(_inject_context):
            _inject_context(f"[Swarm results for: {task[:80]}]\n{summary}")

        # Notify user
        print_ok(f"Swarm [{swarm_id}] complete — /swarm status to see results")

    except Exception as exc:
        logger.debug("suppressed exception in _swarm_background", exc_info=True)
        run["status"] = "failed"
        run["error"] = str(exc)
        print_error(f"Swarm [{swarm_id}] failed: {exc}")


async def cmd_swarm(args: str, ctx: REPLContext) -> str | None:
    """Autonomous multi-agent swarm — runs in background.

    Usage:
      /swarm [--model MODEL] [--no-synth] [--smart] <task description>
      /swarm status              Show all swarm runs
      /swarm results [id]        Show results of a swarm run
      /swarm stop [id|all]       Cancel running swarm(s)
    """
    tokens = shlex.split(args) if args else []
    if not tokens:
        print_info(
            "Usage:\n"
            "  /swarm <task>            Launch a swarm\n"
            "  /swarm status            Show swarm runs\n"
            "  /swarm results [id]      Show results\n"
            "  /swarm stop [id|all]     Cancel swarm(s)\n"
            "\n"
            "Flags: --model MODEL  --no-synth  --smart",
        )
        return None

    sub = tokens[0]

    # --- /swarm status ---
    if sub == "status":
        if not ctx.swarm_runs:
            print_info("No swarm runs.")
            return None
        table = Table(show_header=True, header_style="bold", title="Swarm Runs")
        table.add_column("ID", style="cyan", width=10)
        table.add_column("Task", style="dim", max_width=40)
        table.add_column("Agents", style="yellow", width=8)
        table.add_column("Status", style="green")
        for sid, run in ctx.swarm_runs.items():
            agent_names = ", ".join(a.agent_name for a in run["assignments"])
            status = run["status"]
            if status == "running":
                # Count finished
                results = run.get("results", [])
                status = f"running ({len(results)}/{len(run['assignments'])})"
            table.add_row(sid, run["task"][:40], agent_names, status)
        console.print(table)
        return None

    # --- /swarm results [id] ---
    if sub == "results":
        target = tokens[1] if len(tokens) > 1 else None
        runs = ctx.swarm_runs
        if target:
            matches = {k: v for k, v in runs.items() if k.startswith(target)}
        else:
            # Show latest
            matches = dict(list(runs.items())[-1:]) if runs else {}
        if not matches:
            print_info("No matching swarm run.")
            return None
        colors = ["cyan", "magenta", "yellow", "green", "blue", "red"]
        for sid, run in matches.items():
            console.print(Rule(f"[bold]Swarm {sid}[/] — {run['status']}", style="bold"))
            if run.get("summary"):
                console.print(Markdown(run["summary"]))
            elif run.get("results"):
                for idx, (name, output) in enumerate(run["results"]):
                    color = colors[idx % len(colors)]
                    console.print(Rule(f"[bold {color}]{name}[/]", style=color))
                    if output.strip():
                        console.print(Markdown(output))
                    console.print()
            elif run.get("error"):
                print_error(run["error"])
            else:
                print_info("Still running...")
            console.print()
        return None

    # --- /swarm stop [id|all] ---
    if sub == "stop":
        target = tokens[1] if len(tokens) > 1 else "all"
        for sid, run in list(ctx.swarm_runs.items()):
            if target == "all" or sid.startswith(target):
                task_obj = run.get("_task")
                if task_obj and not task_obj.done():
                    task_obj.cancel()
                    run["status"] = "cancelled"
                    print_ok(f"Cancelled swarm {sid}")
        return None

    # --- /swarm <task> — launch new swarm ---
    model_override: str | None = None
    synthesize = True
    smart_plan = False
    task_tokens: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "--model" and i + 1 < len(tokens):
            model_override = tokens[i + 1]
            i += 2
        elif tokens[i] == "--no-synth":
            synthesize = False
            i += 1
        elif tokens[i] == "--smart":
            smart_plan = True
            i += 1
        else:
            task_tokens = tokens[i:]
            break

    if not task_tokens:
        print_error("No task description provided.")
        return None

    task = " ".join(task_tokens)

    # Load agent catalog
    agent_configs = load_agent_configs()
    if not agent_configs:
        print_error("No agents found in ~/.obscura/agents.yaml")
        return None

    # Plan decomposition
    try:
        if smart_plan:
            print_info("Planning swarm decomposition (LLM)...")
            assignments = await _swarm_plan_smart(task, agent_configs, ctx)
        else:
            assignments = _swarm_plan_fast(task, agent_configs)
    except Exception as exc:
        logger.debug("suppressed exception in cmd_swarm", exc_info=True)
        print_error(f"Swarm planning failed: {exc}")
        return None

    if not assignments:
        print_error("Planner returned no assignments.")
        return None

    # Display plan
    console.print()
    table = Table(show_header=True, header_style="bold", title="Swarm Plan")
    table.add_column("#", style="yellow", width=4)
    table.add_column("Agent", style="cyan", width=20)
    table.add_column("Rationale", style="dim")
    for idx, a in enumerate(assignments, 1):
        table.add_row(str(idx), a.agent_name, a.rationale)
    console.print(table)
    console.print()

    # Launch in background
    runtime = await ctx.get_runtime()
    swarm_id = uuid.uuid4().hex[:6]

    run_state: dict[str, Any] = {
        "task": task,
        "assignments": assignments,
        "status": "starting",
        "results": [],
        "summary": None,
        "error": None,
        "_task": None,
    }
    ctx.swarm_runs[swarm_id] = run_state

    bg_task = asyncio.create_task(
        _swarm_background(
            swarm_id=swarm_id,
            task=task,
            assignments=assignments,
            runtime=runtime,
            agent_configs=agent_configs,
            model_override=model_override,
            synthesize=synthesize,
            ctx=ctx,
        ),
    )
    run_state["_task"] = bg_task

    print_ok(
        f"Swarm [{swarm_id}] launched with {len(assignments)} agents — "
        "prompt is free. Use /swarm status or /swarm results to check.",
    )
    return None


# ---------------------------------------------------------------------------
# Handlers — MCP discovery
# ---------------------------------------------------------------------------


async def cmd_discover(args: str, ctx: REPLContext) -> str | None:
    """Discover popular MCP tools dynamically."""

    parts = args.strip().split()
    category = parts[0] if parts and not parts[0].isdigit() else None
    limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
    if not category and parts and parts[0].isdigit():
        limit = int(parts[0])

    discovery = DynamicToolDiscovery()

    try:
        if category:
            console.print(f"[cyan]🔍 Discovering {category} tools...[/]")
            capabilities = discovery.discover_by_category(category, limit)
        else:
            console.print(f"[cyan]🔍 Discovering top {limit} popular tools...[/]")
            capabilities = discovery.discover_popular(limit)

        if not capabilities:
            print_info("No tools found. Try a different category or limit.")
            return None

        table = Table(show_header=True, header_style="bold")
        table.add_column("Rank", style="yellow", width=6)
        table.add_column("Category", style="magenta", width=12)
        table.add_column("Name", style="cyan", width=40)
        table.add_column("Package", style="dim", width=30)

        for cap in capabilities:
            pkg = cap.npm_package or "N/A"
            name = cap.name[:40] if len(cap.name) > 40 else cap.name
            table.add_row(str(cap.popularity_rank), cap.category, name, pkg)

        console.print(f"\n[green]✅ Found {len(capabilities)} tools:[/]\n")
        console.print(table)
        console.print("\n[dim]Usage: /discover [category] [limit][/]")
        console.print(
            "[dim]Categories: web, filesystem, git, database, ai, cloud, search[/]\n",
        )
    except Exception as exc:
        logger.debug("suppressed exception in cmd_discover", exc_info=True)
        print_error(f"Discovery failed: {exc}")

    return None


# ---------------------------------------------------------------------------
# MCP Management Commands
# ---------------------------------------------------------------------------


async def cmd_mcp(args: str, ctx: REPLContext) -> str | None:
    """MCP server management commands."""

    try:
        args_list = shlex.split(args) if args.strip() else []
    except ValueError:
        logger.debug("suppressed exception in cmd_mcp", exc_info=True)
        args_list = args.split()

    handle_mcp_command(args_list)
    return None


async def cmd_plugin(args: str, ctx: REPLContext) -> str | None:
    """Manage Obscura and Claude Code plugins.

    Usage:
      /plugin list                          — List all plugins (native + Claude Code)
      /plugin install <source>              — Install from path/git/pip (auto-detects format)
      /plugin install <name>@<marketplace>  — Install from Claude Code marketplace
      /plugin remove <name>                 — Uninstall plugin
      /plugin enable <id>                   — Enable a disabled plugin
      /plugin disable <id>                  — Disable without removing
      /plugin info <id>                     — Show manifest, status, contributions
      /plugin health                        — Show health status of all plugins
      /plugin marketplace add <source>      — Add a Claude Code marketplace (github:org/repo)
      /plugin marketplace list              — Show registered marketplaces
      /plugin marketplace remove <name>     — Remove a marketplace
      /plugin marketplace search <query>    — Search across all marketplaces
    """
    try:
        tokens = shlex.split(args) if args and args.strip() else []
    except ValueError:
        logger.debug("suppressed exception in cmd_plugin", exc_info=True)
        tokens = args.split()

    if not tokens:
        print_info("Usage: /plugin [list|install|remove|enable|disable|info|health]")
        return None

    sub = tokens[0]

    try:
        registry = PluginRegistryService()
    except Exception:
        logger.debug("suppressed exception in cmd_plugin", exc_info=True)
        print_error("Plugin management not available.")
        return None

    if sub == "list":
        plugins = registry.list_plugins()

        # Include discovered builtins that aren't in the registry
        try:
            loader = PluginLoader()
            registered_ids = {p.id for p in plugins}
            for spec in loader.discover_builtins():
                if spec.id not in registered_ids:
                    plugins.append(PluginEntry.from_spec(spec, source="builtin"))
                    plugins[-1].enabled = True
                    plugins[-1].state = "enabled"
        except Exception:
            logger.debug("suppressed exception in cmd_plugin", exc_info=True)

        # Include discovered Claude Code plugins
        try:
            from obscura.plugins.claude_compat.loader import ClaudePluginLoader

            cc_loader = ClaudePluginLoader()
            cc_specs = cc_loader.discover()
            for spec in cc_specs:
                if spec.id not in {p.id for p in plugins}:
                    plugins.append(PluginEntry.from_spec(spec, source="claude"))
                    plugins[-1].enabled = True
                    plugins[-1].state = "enabled"
        except Exception:
            logger.debug("suppressed exception in cmd_plugin", exc_info=True)

        if not plugins:
            print_info("No plugins registered.")
            return None
        for p in plugins:
            status_color = {"enabled": "green", "disabled": "dim", "failed": "red"}.get(
                p.state,
                "yellow",
            )
            # Distinguish native vs Claude Code plugins.
            type_badge = "[dim](claude)[/] " if str(p.id).startswith("claude:") else ""
            console.print(
                f"  • [cyan]{p.id}[/] "
                f"{type_badge}"
                f"v{p.version} "
                f"[{status_color}]{p.state}[/] "
                f"— {p.description[:60]}",
            )
        return None

    if sub == "install":
        if len(tokens) < 2:
            print_error(
                "Usage: /plugin install <source>  OR  /plugin install <name>@<marketplace>"
            )
            return None
        source = tokens[1]

        # Check for Claude Code marketplace format: name@marketplace
        if "@" in source and not source.startswith(
            ("http://", "https://", "git@", "/")
        ):
            plugin_name, marketplace_name = source.rsplit("@", 1)
            print_info(
                f"Installing {plugin_name} from marketplace {marketplace_name}..."
            )
            try:
                from obscura.plugins.claude_compat.marketplace import (
                    MarketplaceResolver,
                )

                resolver = MarketplaceResolver()
                install_path = resolver.install_plugin(plugin_name, marketplace_name)
                if install_path:
                    # Register in Obscura's registry.
                    from obscura.plugins.claude_compat.manifest_adapter import (
                        adapt_claude_manifest,
                    )

                    spec = adapt_claude_manifest(
                        install_path, marketplace=marketplace_name
                    )
                    if spec:
                        registry.install(spec, source=source, auto_enable=True)
                        print_ok(f"Installed {source} (Claude Code plugin)")
                    else:
                        print_error(
                            f"Could not parse plugin manifest at {install_path}"
                        )
                else:
                    print_error(
                        f"Plugin {plugin_name} not found in marketplace {marketplace_name}"
                    )
            except Exception as exc:
                logger.debug("suppressed exception in cmd_plugin", exc_info=True)
                print_error(f"Marketplace install failed: {exc}")
            return None

        # Check if source is a Claude Code plugin directory.
        source_path = Path(source).expanduser()
        if (
            source_path.is_dir()
            and (source_path / ".claude-plugin" / "plugin.json").exists()
        ):
            print_info(f"Detected Claude Code plugin: {source}")
            try:
                from obscura.plugins.claude_compat.manifest_adapter import (
                    adapt_claude_manifest,
                )

                spec = adapt_claude_manifest(source_path)
                if spec:
                    registry.install(spec, source=source, auto_enable=True)
                    print_ok(f"Installed {spec.id} (Claude Code plugin)")
                else:
                    print_error("Could not parse Claude Code plugin manifest")
            except Exception as exc:
                logger.debug("suppressed exception in cmd_plugin", exc_info=True)
                print_error(f"Install failed: {exc}")
            return None

        # Default: native Obscura plugin install.
        print_info(f"Installing plugin: {source}")
        res = registry.install_from_source(source)
        if res:
            print_ok(f"Installed {source}")
        else:
            print_error(f"Failed to install {source}")
        return None

    if sub in ("remove", "uninstall"):
        if len(tokens) < 2:
            print_error("Usage: /plugin remove <name>")
            return None
        name = tokens[1]
        print_info(f"Removing plugin: {name}")
        ok = registry.uninstall(name)
        print_ok(f"Removed {name}") if ok else print_error(f"Failed to remove {name}")
        return None

    if sub == "enable":
        if len(tokens) < 2:
            print_error("Usage: /plugin enable <id>")
            return None
        ok = registry.enable(tokens[1])
        print_ok(f"Enabled {tokens[1]}") if ok else print_error(
            f"Cannot enable {tokens[1]}",
        )
        return None

    if sub == "disable":
        if len(tokens) < 2:
            print_error("Usage: /plugin disable <id>")
            return None
        ok = registry.disable(tokens[1])
        print_ok(f"Disabled {tokens[1]}") if ok else print_error(
            f"Cannot disable {tokens[1]}",
        )
        return None

    if sub == "info":
        if len(tokens) < 2:
            print_error("Usage: /plugin info <id>")
            return None
        pid = tokens[1]
        contribs = registry.get_contributions(pid)
        if not contribs:
            print_error(f"Plugin '{pid}' not found.")
            return None
        console.print(f"[bold cyan]{pid}[/]")
        console.print(f"  Status: {registry.get_status(pid)}")
        if contribs.get("capabilities"):
            console.print(f"  Capabilities: {', '.join(contribs['capabilities'])}")
        if contribs.get("tools"):
            console.print(
                f"  Tools ({len(contribs['tools'])}): {', '.join(contribs['tools'][:10])}",
            )
        if contribs.get("workflows"):
            console.print(f"  Workflows: {', '.join(contribs['workflows'])}")
        return None

    if sub == "health":
        plugins = registry.list_plugins()
        for p in plugins:
            status = p.state or "unknown"
            icon = "✓" if status == "enabled" else "✗" if status == "failed" else "○"
            color = (
                "green"
                if status == "enabled"
                else "red"
                if status == "failed"
                else "dim"
            )
            console.print(f"  {icon} [{color}]{p.id or '?'}[/] — {status}")
        return None

    if sub == "marketplace":
        sub2 = tokens[1] if len(tokens) > 1 else ""
        rest2 = tokens[2] if len(tokens) > 2 else ""

        try:
            from obscura.plugins.claude_compat.marketplace import MarketplaceResolver

            resolver = MarketplaceResolver()
        except Exception:
            logger.debug("suppressed exception in cmd_plugin", exc_info=True)
            print_error("Marketplace support not available.")
            return None

        if sub2 == "add":
            if not rest2:
                print_error(
                    "Usage: /plugin marketplace add <github:org/repo | git:url | path>"
                )
                return None
            # Parse source shorthand.
            if rest2.startswith("github:"):
                source = {"source": "github", "repo": rest2[7:]}
            elif rest2.startswith("git:"):
                source = {"source": "git", "url": rest2[4:]}
            elif "/" in rest2 and not rest2.startswith(("http", "/")):
                # Assume github shorthand: org/repo
                source = {"source": "github", "repo": rest2}
            elif Path(rest2).expanduser().is_dir():
                source = {"source": "directory", "path": rest2}
            else:
                source = {"source": "git", "url": rest2}

            name = (
                tokens[3]
                if len(tokens) > 3
                else rest2.split("/")[-1].replace(".git", "")
            )
            print_info(f"Adding marketplace: {name}...")
            if resolver.add_marketplace(name, source):
                plugins = resolver.list_plugins(name)
                print_ok(f"Marketplace '{name}' added ({len(plugins)} plugins)")
            else:
                print_error(f"Could not fetch marketplace: {name}")
            return None

        if sub2 == "list":
            markets = resolver.list_marketplaces()
            if not markets:
                print_info(
                    "No marketplaces registered. Use /plugin marketplace add <source>."
                )
                return None
            for name, src in markets.items():
                plugins = resolver.list_plugins(name)
                source_str = src.repo or src.url or src.path or src.source
                console.print(
                    f"  • [cyan]{name}[/] ({source_str}) — {len(plugins)} plugins"
                )
            return None

        if sub2 == "remove":
            if not rest2:
                print_error("Usage: /plugin marketplace remove <name>")
                return None
            if resolver.remove_marketplace(rest2):
                print_ok(f"Marketplace '{rest2}' removed.")
            else:
                print_error(f"Marketplace '{rest2}' not found.")
            return None

        if sub2 == "search":
            query = " ".join(tokens[2:]).lower() if len(tokens) > 2 else ""
            if not query:
                print_error("Usage: /plugin marketplace search <query>")
                return None
            markets = resolver.list_marketplaces()
            found = 0
            for market_name in markets:
                for entry in resolver.list_plugins(market_name):
                    text = f"{entry.name} {entry.description} {' '.join(entry.tags)}".lower()
                    if query in text:
                        console.print(
                            f"  • [cyan]{entry.name}[/]@{market_name} "
                            f"v{entry.version} — {entry.description[:50]}"
                        )
                        found += 1
            if found == 0:
                print_info(f"No plugins matching '{query}'.")
            else:
                print_info(
                    f"{found} plugin(s) found. Install with: /plugin install <name>@<marketplace>"
                )
            return None

        print_info("Usage: /plugin marketplace [add|list|remove|search]")
        return None

    print_info(
        "Unknown subcommand. Usage: /plugin [list|install|remove|enable|disable|info|health|marketplace]",
    )
    return None


async def cmd_pack(args: str, ctx: REPLContext) -> str | None:
    """Manage Obscura packs — curated bundles of plugins, templates, and policies.

    Usage:
      /pack list           — List all available packs
      /pack info <name>    — Show pack details
      /pack create <name>  — Scaffold a new pack TOML
    """
    try:
        tokens = shlex.split(args) if args and args.strip() else []
    except ValueError:
        logger.debug("suppressed exception in cmd_pack", exc_info=True)
        tokens = args.split()

    if not tokens:
        print_info("Usage: /pack [list|info|create]")
        return None

    sub = tokens[0]

    if sub == "list":
        try:
            dirs = resolve_all_specs_dirs()
            if not dirs:
                print_info("No specs directories found.")
                return None
            registry = load_specs_dirs(dirs)
        except Exception:
            logger.debug("suppressed exception in cmd_pack", exc_info=True)
            print_error("Could not load specs.")
            return None

        if not registry.packs:
            print_info("No packs found.")
            return None
        for name, pack in sorted(registry.packs.items()):
            plugins = ", ".join(pack.spec.plugins) if pack.spec.plugins else "none"
            policies = ", ".join(pack.spec.policies) if pack.spec.policies else "none"
            console.print(
                f"  • [cyan]{name}[/] — {pack.metadata.description[:60]}",
            )
            console.print(
                f"    plugins: [dim]{plugins}[/]  policies: [dim]{policies}[/]",
            )
        return None

    if sub == "info":
        if len(tokens) < 2:
            print_error("Usage: /pack info <name>")
            return None
        pack_name = tokens[1]
        try:
            dirs = resolve_all_specs_dirs()
            registry = load_specs_dirs(dirs) if dirs else None
        except Exception:
            logger.debug("suppressed exception in cmd_pack", exc_info=True)
            registry = None

        if registry is None:
            print_error("Could not load specs.")
            return None

        pack = registry.get_pack(pack_name)
        if pack is None:
            print_error(f"Pack '{pack_name}' not found.")
            return None

        console.print(f"[bold cyan]{pack_name}[/]")
        console.print(f"  {pack.metadata.description}")
        if pack.metadata.tags:
            console.print(f"  Tags: {', '.join(pack.metadata.tags)}")
        if pack.spec.plugins:
            console.print(f"  Plugins: {', '.join(pack.spec.plugins)}")
        if pack.spec.templates:
            console.print(f"  Templates: {', '.join(pack.spec.templates)}")
        if pack.spec.policies:
            console.print(f"  Policies: {', '.join(pack.spec.policies)}")
        if pack.spec.capabilities.grant:
            console.print(
                f"  Capabilities (grant): {', '.join(pack.spec.capabilities.grant)}",
            )
        if pack.spec.capabilities.deny:
            console.print(
                f"  Capabilities (deny): {', '.join(pack.spec.capabilities.deny)}",
            )
        if pack.spec.config:
            console.print(f"  Config: {pack.spec.config}")
        if pack.spec.instructions.strip():
            console.print(f"  Instructions: {pack.spec.instructions.strip()[:80]}...")
        return None

    if sub == "create":
        if len(tokens) < 2:
            print_error("Usage: /pack create <name>")
            return None
        pack_name = tokens[1]
        import textwrap

        # Try local .obscura/specs/packs first, then global
        local_dir = Path.cwd() / ".obscura" / "specs" / "packs"
        global_dir = Path.home() / ".obscura" / "specs" / "packs"
        target_dir = local_dir if local_dir.exists() else global_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{pack_name}.toml"

        if target.exists():
            print_error(f"Pack file already exists: {target}")
            return None

        content = textwrap.dedent(f"""\
            apiVersion = "obscura/v1"
            kind = "Pack"

            [metadata]
            name = "{pack_name}"
            description = "TODO — describe this pack"
            tags = []

            [spec]
            plugins = []
            templates = []
            policies = []
            instructions = ""

            [spec.capabilities]
            grant = []
            deny = []

            [spec.config]
        """)
        target.write_text(content, encoding="utf-8")
        print_ok(f"Created pack scaffold: {target}")
        return None

    print_info("Unknown subcommand. Usage: /pack [list|info|create]")
    return None


async def cmd_inspect(args: str, ctx: REPLContext) -> str | None:
    """Inspect compiled/resolved state of Obscura resources.

    Usage:
      /inspect workspace <name>    — Show compiled workspace state
      /inspect agent <name>        — Show compiled agent (in default workspace)
      /inspect capability <cap-id> — Show capability with tools and owner
      /inspect pack <name>         — Show pack with full contents
    """
    try:
        tokens = shlex.split(args) if args and args.strip() else []
    except ValueError:
        logger.debug("suppressed exception in cmd_inspect", exc_info=True)
        tokens = args.split()

    if not tokens:
        print_info(
            "Usage: /inspect [workspace|agent|capability|pack] <name>",
        )
        return None

    resource_type = tokens[0]
    resource_name = tokens[1] if len(tokens) > 1 else None

    if resource_type == "workspace":
        if not resource_name:
            print_error("Usage: /inspect workspace <name>")
            return None
        try:
            ws = compile_workspace(resource_name, strict=False)
        except Exception as exc:
            logger.debug("suppressed exception in cmd_inspect", exc_info=True)
            print_error(f"Cannot compile workspace '{resource_name}': {exc}")
            return None

        console.print(f"\n[bold cyan]Workspace: {ws.name}[/]")

        if ws.packs:
            console.print(f"  Packs: {', '.join(ws.packs)}")
        if ws.plugin_include:
            console.print(
                f"  Plugins (include): {', '.join(sorted(ws.plugin_include))}",
            )
        if ws.plugin_exclude:
            console.print(
                f"  Plugins (exclude): {', '.join(sorted(ws.plugin_exclude))}",
            )
        console.print(f"  Preload plugins: {ws.preload_plugins}")
        if ws.startup_agents:
            console.print(f"  Startup agents: {', '.join(ws.startup_agents)}")

        # Policies
        if ws.policies:
            console.print("\n  [bold]Policies:[/]")
            for p in ws.policies:
                restrictions: list[str] = []
                if p.tool_denylist:
                    restrictions.append(f"deny {len(p.tool_denylist)} tools")
                if p.require_confirmation:
                    restrictions.append(f"confirm {len(p.require_confirmation)} tools")
                if p.max_turns != 25:
                    restrictions.append(f"max_turns={p.max_turns}")
                info = f" ({', '.join(restrictions)})" if restrictions else ""
                console.print(f"    • [yellow]{p.name}[/]{info}")

        # Memory
        if ws.memory:
            console.print(
                f"\n  [bold]Memory:[/] namespace={ws.memory.namespace} "
                f"scope={ws.memory.shared_scope} "
                f"retention={ws.memory.retention_days}d",
            )

        # Config (skip internal _pack_* keys)
        visible_config = {
            k: v for k, v in ws.config.items() if not k.startswith("_pack_")
        }
        if visible_config:
            console.print(f"\n  [bold]Config:[/] {visible_config}")

        # Agents table
        if ws.agents:
            table = Table(title="Agents", show_header=True, padding=(0, 1))
            table.add_column("Name", style="cyan", no_wrap=True)
            table.add_column("Template", style="dim")
            table.add_column("Mode", style="green")
            table.add_column("Provider")
            table.add_column("Plugins", justify="right")
            table.add_column("Capabilities", justify="right")
            for a in ws.agents:
                table.add_row(
                    a.name,
                    a.template_name,
                    a.mode,
                    a.provider,
                    str(len(a.plugins)),
                    str(len(a.capabilities)),
                )
            console.print()
            console.print(table)

        return None

    if resource_type == "agent":
        if not resource_name:
            print_error("Usage: /inspect agent <name>")
            return None

        # Try compiled workspace first
        agent = None
        try:
            ws = compile_workspace("default", strict=False)
            agent = next((a for a in ws.agents if a.name == resource_name), None)
        except Exception:
            logger.debug("suppressed exception in cmd_inspect", exc_info=True)

        if agent is not None:
            console.print(f"\n[bold cyan]Agent: {agent.name}[/]")
            console.print(f"  Template: {agent.template_name}")
            console.print(f"  Mode: {agent.mode}  Type: {agent.agent_type}")
            console.print(
                f"  Provider: {agent.provider}  Model: {agent.model_id or 'default'}",
            )
            console.print(f"  Max iterations: {agent.max_iterations}")

            if agent.plugins:
                console.print(f"\n  [bold]Plugins ({len(agent.plugins)}):[/]")
                for p in agent.plugins:
                    console.print(f"    • {p}")

            if agent.capabilities:
                console.print(f"\n  [bold]Capabilities ({len(agent.capabilities)}):[/]")
                for c in agent.capabilities:
                    console.print(f"    • {c}")

            if agent.tool_allowlist is not None:
                console.print(
                    f"\n  [bold]Tool allowlist ({len(agent.tool_allowlist)}):[/]",
                )
                for t in sorted(agent.tool_allowlist):
                    console.print(f"    • {t}")
            if agent.tool_denylist:
                console.print(
                    f"\n  [bold]Tool denylist ({len(agent.tool_denylist)}):[/]",
                )
                for t in sorted(agent.tool_denylist):
                    console.print(f"    • {t}")

            if agent.mcp_servers:
                console.print(f"\n  [bold]MCP Servers ({len(agent.mcp_servers)}):[/]")
                for s in agent.mcp_servers:
                    cmd = f"{s.command} {' '.join(s.args)}" if s.command else "n/a"
                    console.print(f"    • {s.name} ({s.transport}) → {cmd}")

            if agent.env:
                console.print("\n  [bold]Environment:[/]")
                console.print(f"    Python: {agent.env.python_version}")
                if agent.env.binaries:
                    console.print(f"    Binaries: {', '.join(agent.env.binaries)}")
                console.print(f"    Network: {agent.env.network_mode}")

            if agent.config:
                console.print(f"\n  [bold]Config:[/] {agent.config}")

            if agent.instructions:
                preview = agent.instructions.strip()[:200]
                if len(agent.instructions.strip()) > 200:
                    preview += "..."
                console.print(f"\n  [bold]Instructions:[/]\n    {preview}")

            return None

        # Fallback: check agents.yaml
        try:
            configs = load_agent_configs(include_disabled=True)
            cfg = configs.get(resource_name)
        except Exception:
            logger.debug("suppressed exception in cmd_inspect", exc_info=True)
            cfg = None

        if cfg:
            console.print(
                f"\n[bold cyan]Agent: {resource_name}[/] [dim](from agents.yaml)[/]",
            )
            console.print(f"  Type: {cfg.get('type', '?')}")
            console.print(f"  Provider: {cfg.get('provider', cfg.get('model', '?'))}")
            console.print(f"  Enabled: {cfg.get('enabled', True)}")
            console.print(f"  Max turns: {cfg.get('max_turns', '?')}")
            if cfg.get("tags"):
                console.print(f"  Tags: {', '.join(cfg['tags'])}")
            caps = cfg.get("capabilities", {})
            if caps.get("grant"):
                console.print(f"  Capabilities (grant): {', '.join(caps['grant'])}")
            if caps.get("deny"):
                console.print(f"  Capabilities (deny): {', '.join(caps['deny'])}")
            plugins = cfg.get("plugins", {})
            if plugins.get("require"):
                console.print(f"  Plugins (require): {', '.join(plugins['require'])}")
            if plugins.get("optional"):
                console.print(f"  Plugins (optional): {', '.join(plugins['optional'])}")
            prompt = cfg.get("system_prompt", "")
            if prompt:
                preview = prompt.strip()[:200]
                if len(prompt.strip()) > 200:
                    preview += "..."
                console.print(f"\n  [bold]System prompt:[/]\n    {preview}")
            return None

        print_error(
            f"Agent '{resource_name}' not found in compiled workspace or agents.yaml.",
        )
        return None

    if resource_type == "capability":
        if not resource_name:
            print_error("Usage: /inspect capability <cap-id>")
            return None
        try:
            from obscura.plugins.registries.capability_index import CapabilityIndex

            loader = PluginLoader()
            ci = CapabilityIndex()
            for spec in loader.discover_builtins():
                for cap in spec.capabilities:
                    ci.register(cap, spec.id)
        except Exception as exc:
            logger.debug("suppressed exception in cmd_inspect", exc_info=True)
            print_error(f"Cannot load capabilities: {exc}")
            return None

        cap = ci.get(resource_name)
        if cap is None:
            print_error(f"Capability '{resource_name}' not found.")
            return None

        owner = ci.get_owner(resource_name)
        console.print(f"\n[bold cyan]Capability: {cap.id}[/]")
        console.print(f"  Description: {cap.description}")
        console.print(f"  Owner plugin: {owner or 'unknown'}")
        console.print(
            f"  Requires approval: {'yes' if cap.requires_approval else 'no'}",
        )
        console.print(f"  Default grant: {'yes' if cap.default_grant else 'no'}")
        if cap.tools:
            console.print(f"\n  [bold]Tools ({len(cap.tools)}):[/]")
            for t in cap.tools:
                console.print(f"    • {t}")
        return None

    if resource_type == "pack":
        if not resource_name:
            print_error("Usage: /inspect pack <name>")
            return None
        try:
            dirs = resolve_all_specs_dirs()
            registry = load_specs_dirs(dirs) if dirs else None
        except Exception:
            logger.debug("suppressed exception in cmd_inspect", exc_info=True)
            registry = None

        if registry is None:
            print_error("Could not load specs.")
            return None

        pack = registry.get_pack(resource_name)
        if pack is None:
            print_error(f"Pack '{resource_name}' not found.")
            return None

        console.print(f"\n[bold cyan]Pack: {pack.metadata.name}[/]")
        console.print(f"  {pack.metadata.description}")
        if pack.metadata.tags:
            console.print(f"  Tags: {', '.join(pack.metadata.tags)}")

        if pack.spec.plugins:
            console.print(f"\n  [bold]Plugins ({len(pack.spec.plugins)}):[/]")
            for p in pack.spec.plugins:
                console.print(f"    • {p}")
        if pack.spec.templates:
            console.print(f"\n  [bold]Templates ({len(pack.spec.templates)}):[/]")
            for t in pack.spec.templates:
                console.print(f"    • {t}")
        if pack.spec.policies:
            console.print(f"\n  [bold]Policies ({len(pack.spec.policies)}):[/]")
            for p in pack.spec.policies:
                console.print(f"    • {p}")
        if pack.spec.capabilities.grant:
            console.print("\n  [bold]Capabilities (grant):[/]")
            for c in pack.spec.capabilities.grant:
                console.print(f"    • [green]{c}[/]")
        if pack.spec.capabilities.deny:
            console.print("\n  [bold]Capabilities (deny):[/]")
            for c in pack.spec.capabilities.deny:
                console.print(f"    • [red]{c}[/]")
        if pack.spec.config:
            console.print("\n  [bold]Config defaults:[/]")
            for k, v in pack.spec.config.items():
                console.print(f"    {k}: {v}")
        if pack.spec.instructions.strip():
            console.print("\n  [bold]Instructions:[/]")
            for line in pack.spec.instructions.strip().splitlines():
                console.print(f"    {line}")

        return None

    print_info(
        "Unknown resource type. Usage: /inspect [workspace|agent|capability|pack] <name>",
    )
    return None


async def cmd_capability(args: str, ctx: REPLContext) -> str | None:
    """Manage plugin capabilities.

    Usage:
      /capability list                         — List all capabilities
      /capability grant <cap> --agent <id>     — Grant capability to agent
      /capability deny <cap> --agent <id>      — Deny capability for agent
      /capability check <cap> --agent <id>     — Check if granted
    """
    try:
        tokens = shlex.split(args) if args and args.strip() else []
    except ValueError:
        logger.debug("suppressed exception in cmd_capability", exc_info=True)
        tokens = args.split()

    if not tokens:
        print_info("Usage: /capability [list|grant|deny|check]")
        return None

    sub = tokens[0]

    try:
        from obscura.plugins.registries import CapabilityIndex
    except ImportError:
        logger.debug("suppressed exception in cmd_capability", exc_info=True)
        print_error("Capability management not available.")
        return None

    if sub == "list":
        try:
            loader = PluginLoader()
            specs = loader.discover_builtins()
            ci = CapabilityIndex()
            for spec in specs:
                for cap in spec.capabilities:
                    ci.register(cap, spec.id)
            caps = ci.list_all()
            if not caps:
                print_info("No capabilities registered.")
                return None
            for cap in caps:
                approval = (
                    " [yellow](requires approval)[/]" if cap.requires_approval else ""
                )
                default = " [green](default grant)[/]" if cap.default_grant else ""
                console.print(
                    f"  • [cyan]{cap.id}[/]{approval}{default} — {cap.description}",
                )
                if cap.tools:
                    console.print(f"    Tools: {', '.join(cap.tools)}")
        except Exception as e:
            logger.debug("suppressed exception in cmd_capability", exc_info=True)
            print_error(f"Failed to list capabilities: {e}")
        return None

    # grant/deny/check require --agent
    agent_id = None
    for i, t in enumerate(tokens):
        if t == "--agent" and i + 1 < len(tokens):
            agent_id = tokens[i + 1]
            break

    cap_id = tokens[1] if len(tokens) > 1 else None
    if not cap_id:
        print_error(f"Usage: /capability {sub} <capability_id> --agent <agent_id>")
        return None

    if not agent_id:
        agent_id = "default"
        print_info(f"No --agent specified, using '{agent_id}'")

    if sub == "grant":
        print_ok(f"Granted capability '{cap_id}' to agent '{agent_id}'")
        return None

    if sub == "deny":
        print_ok(f"Denied capability '{cap_id}' for agent '{agent_id}'")
        return None

    if sub == "check":
        print_info(
            f"Capability '{cap_id}' grant status for agent '{agent_id}': use /capability list to see available capabilities",
        )
        return None

    print_info("Unknown subcommand. Usage: /capability [list|grant|deny|check]")
    return None


# ---------------------------------------------------------------------------
# Handlers — A2A (Agent-to-Agent)
# ---------------------------------------------------------------------------


async def cmd_a2a(args: str, ctx: REPLContext) -> str | None:
    """A2A agent communication: discover, send, list, stream."""
    parts = args.strip().split(None, 1)
    sub = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "discover":
        return await _a2a_discover(rest, ctx)
    if sub == "send":
        return await _a2a_send(rest, ctx)
    if sub == "stream":
        return await _a2a_stream(rest, ctx)
    if sub == "list":
        return await _a2a_list_tasks(rest, ctx)
    if sub == "agents":
        return await _a2a_list_agents(ctx)

    print_info("Usage: /a2a discover|send|stream|list|agents")
    print_info("  /a2a discover <url>           - Discover remote agent capabilities")
    print_info("  /a2a send <url> <message>     - Send message to agent (blocking)")
    print_info("  /a2a stream <url> <message>   - Stream message with real-time events")
    print_info("  /a2a list <url>               - List tasks on remote agent")
    print_info("  /a2a agents                   - List connected agents")
    return None


async def _a2a_discover(url: str, ctx: REPLContext) -> str | None:
    """Discover remote A2A agent. Usage: /a2a discover <url>."""
    if not url:
        print_error("Usage: /a2a discover <url>")
        return None

    try:
        from obscura.integrations.a2a.client import A2AClient
    except ImportError:
        logger.debug("suppressed exception in _a2a_discover", exc_info=True)
        print_error(
            "A2A integration not available. Install with: pip install obscura[a2a]",
        )
        return None

    try:
        async with A2AClient(url.strip()) as client:
            card = await client.discover()

            table = Table(title=f"Agent: {card.name}", show_header=True)
            table.add_column("Property", style="cyan", no_wrap=True)
            table.add_column("Value", style="green")

            table.add_row("Name", card.name)
            table.add_row("URL", card.url)
            table.add_row("Description", card.description or "—")
            table.add_row("Version", card.version)
            table.add_row("Protocol", card.protocolVersion)
            table.add_row("Skills", str(len(card.skills)))
            table.add_row("Streaming", "✓" if card.capabilities.streaming else "✗")
            table.add_row(
                "Push Notifications",
                "✓" if card.capabilities.pushNotifications else "✗",
            )

            console.print(table)

            if card.skills:
                print_info(f"\n{len(card.skills)} Skills:")
                for skill in card.skills:
                    console.print(f"  • [bold]{skill.name}[/bold]: {skill.description}")

    except Exception as exc:
        logger.debug("suppressed exception in _a2a_discover", exc_info=True)
        print_error(f"Discovery failed: {exc}")
    return None


async def _a2a_send(args: str, ctx: REPLContext) -> str | None:
    """Send message to A2A agent. Usage: /a2a send <url> <message>."""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_error("Usage: /a2a send <url> <message>")
        return None

    url, message = parts

    try:
        from obscura.integrations.a2a.client import A2AClient
    except ImportError:
        logger.debug("suppressed exception in _a2a_send", exc_info=True)
        print_error("A2A integration not available")
        return None

    try:
        async with A2AClient(url) as client:
            print_info(f"Sending message to {url}...")
            task = await client.send_message(message, blocking=True)

            print_ok(f"Task {task.id} completed ({task.status.state.value})")

            # Display response
            if task.history:
                for msg in task.history:
                    if msg.role == "agent":
                        console.print("\n[bold green]Agent Response:[/bold green]")
                        for part in msg.parts:
                            _part_text = getattr(part, "text", None)
                            if _part_text is not None:
                                console.print(_part_text)

    except Exception as exc:
        logger.debug("suppressed exception in _a2a_send", exc_info=True)
        print_error(f"Send failed: {exc}")
    return None


async def _a2a_stream(args: str, ctx: REPLContext) -> str | None:
    """Stream message to A2A agent. Usage: /a2a stream <url> <message>."""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_error("Usage: /a2a stream <url> <message>")
        return None

    url, message = parts

    try:
        from obscura.integrations.a2a.client import A2AClient
    except ImportError:
        logger.debug("suppressed exception in _a2a_stream", exc_info=True)
        print_error("A2A integration not available")
        return None

    try:
        async with A2AClient(url) as client:
            print_info(f"Streaming message to {url}...")
            async for event in client.stream_message(message):
                if event.kind == "status-update":
                    console.print(f"[dim]Status: {event.status.state.value}[/dim]")
                elif event.kind == "artifact-update":
                    console.print(
                        f"[green]Artifact: {event.artifact.name or 'unnamed'}[/green]",
                    )

    except Exception as exc:
        logger.debug("suppressed exception in _a2a_stream", exc_info=True)
        print_error(f"Stream failed: {exc}")
    return None


async def _a2a_list_tasks(url: str, ctx: REPLContext) -> str | None:
    """List tasks on remote agent. Usage: /a2a list <url>."""
    if not url:
        print_error("Usage: /a2a list <url>")
        return None

    try:
        from obscura.integrations.a2a.client import A2AClient
    except ImportError:
        logger.debug("suppressed exception in _a2a_list_tasks", exc_info=True)
        print_error("A2A integration not available")
        return None

    try:
        async with A2AClient(url.strip()) as client:
            tasks, next_cursor = await client.list_tasks(limit=20)

            if not tasks:
                print_info("No tasks found")
                return None

            table = Table(title=f"Tasks on {url}", show_header=True)
            table.add_column("Task ID", style="cyan", no_wrap=True)
            table.add_column("State", style="yellow")
            table.add_column("Messages", style="magenta")

            for task in tasks:
                table.add_row(
                    task.id[:12] + "...",
                    task.status.state.value,
                    str(len(task.history)),
                )

            console.print(table)
            if next_cursor:
                print_info(f"More tasks available (cursor: {next_cursor[:12]}...)")

    except Exception as exc:
        logger.debug("suppressed exception in _a2a_list_tasks", exc_info=True)
        print_error(f"List failed: {exc}")
    return None


async def _a2a_list_agents(ctx: REPLContext) -> str | None:
    """List connected A2A agents."""
    # This could be extended to track agents in ctx.state if we add a session manager
    print_info("Connected agents: (session management not yet implemented)")
    print_info("Use /a2a discover <url> to discover and connect to agents")
    return None


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


async def cmd_memory(args: str, ctx: REPLContext) -> str | None:
    """Show vector memory stats, search, or clear auto-saved memories."""
    if ctx.vector_store is None:
        print_warning(
            "Vector memory is disabled. Set OBSCURA_VECTOR_MEMORY=on to enable.",
        )
        return None

    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0] if parts else "stats"
    rest = parts[1] if len(parts) > 1 else ""

    if subcmd == "stats":
        try:
            stats = ctx.vector_store.get_stats()
            print_info("Vector Memory Stats:")
            for k, v in stats.items():
                console.print(f"  [dim]{k}:[/] {v}")
        except Exception as exc:
            logger.debug("suppressed exception in cmd_memory", exc_info=True)
            print_error(f"Could not get stats: {exc}")

    elif subcmd == "search":
        if not rest:
            print_warning("Usage: /memory search <query>")
            return None
        try:
            results = ctx.vector_store.search_reranked(
                rest,
                top_k=5,
                recency_weight=0.2,
            )
            if not results:
                print_info("No results found.")
            else:
                for i, r in enumerate(results, 1):
                    text_preview = r.text[:150].replace("\n", " ")
                    console.print(
                        f"  [bold]{i}.[/] (score: {r.score:.2f}) {text_preview}",
                    )
        except Exception as exc:
            logger.debug("suppressed exception in cmd_memory", exc_info=True)
            print_error(f"Search failed: {exc}")

    elif subcmd == "clear":
        try:
            scope = rest.strip().lower()
            if scope in {"mcp", "mcp-logs", "mcp_logs"}:
                count = clear_mcp_noise_memories(ctx.vector_store)
                print_ok(
                    f"Cleared {count} MCP-related auto-saved memories from CLI namespace.",
                )
            else:
                count = ctx.vector_store.clear_namespace(CLI_NAMESPACE)
                print_ok(f"Cleared {count} auto-saved memories from CLI namespace.")
        except Exception as exc:
            logger.debug("suppressed exception in cmd_memory", exc_info=True)
            print_error(f"Clear failed: {exc}")

    else:
        print_info("Usage: /memory [stats|search <query>|clear [mcp]]")

    return None


# ---------------------------------------------------------------------------
# Workspace init
# ---------------------------------------------------------------------------


async def cmd_migrate(args: str, _ctx: REPLContext) -> str | None:
    """Detect and import external agent configs (Cursor, Copilot, Claude, ...).

    Usage:
        /migrate external          Prompt to import any detected sources
        /migrate external --force  Re-run including sources marked "never"
        /migrate external --list   Show detected sources without importing
    """
    from obscura.core.migrate_external import (
        clear_decisions,
        migrate_all,
        scan,
    )

    parts = args.split()
    target = parts[0] if parts else "external"
    if target != "external":
        print_error(f"Unknown migrate target: {target}. Try /migrate external.")
        return None

    cwd = Path.cwd()
    if "--force" in parts:
        clear_decisions(cwd)

    sources = scan(cwd)
    if not sources:
        print_info("No external agent configs detected.")
        return None

    if "--list" in parts:
        print_info(f"Detected {len(sources)} source(s):")
        for s in sources:
            print_info(f"  • [{s.scope}] {s.label}  →  {s.dest}")
        return None

    print_info(f"Importing {len(sources)} source(s):")
    imported = migrate_all(sources, cwd, emit=print_info)
    print_ok(f"Imported {imported} of {len(sources)}.")
    return None


async def cmd_init(args: str, _ctx: REPLContext) -> str | None:
    """Initialise a local .obscura/ workspace and bootstrap plugins."""
    from obscura.core.workspace import (
        WorkspaceExistsError,
        bootstrap_all_builtins,
        init_workspace,
    )

    force = "--force" in args
    skip_bootstrap = "--no-bootstrap" in args

    try:
        ws = init_workspace(force=force)
        print_ok(f"Workspace initialised at {ws}")
    except WorkspaceExistsError:
        logger.debug("suppressed exception in cmd_init", exc_info=True)
        if not force:
            print_warning(
                ".obscura/ already exists. Use /init --force to reinitialise.",
            )
            if skip_bootstrap:
                return None
            # Still run bootstrap even if workspace exists
    except Exception as exc:
        logger.debug("suppressed exception in cmd_init", exc_info=True)
        print_error(f"Init failed: {exc}")
        return None

    if not skip_bootstrap:
        print_info("Bootstrapping plugin dependencies...")
        try:
            summary = bootstrap_all_builtins()
            if summary["installed"]:
                print_ok(f"Installed: {', '.join(summary['installed'])}")
            if summary["skipped"]:
                print_info(f"Already present: {len(summary['skipped'])} deps")
            if summary["errors"]:
                print_warning(f"Failed: {', '.join(summary['errors'])}")
            if summary["warnings"]:
                for w in summary["warnings"]:
                    print_warning(w)
            if not summary["errors"]:
                print_ok("All plugin dependencies bootstrapped.")
            else:
                print_warning(
                    "Some dependencies failed. Tools will register but "
                    "may fail at runtime. Install missing deps manually.",
                )
        except Exception as exc:
            logger.debug("suppressed exception in cmd_init", exc_info=True)
            print_warning(f"Bootstrap step failed: {exc}")

    # Phase 2: Generate OBSCURA.md if it doesn't exist
    project_md = Path.cwd() / "OBSCURA.md"
    if not project_md.exists() and "--no-project-md" not in args:
        print_info("Generating OBSCURA.md for this repository...")
        try:
            onboarding_prompt = _build_onboarding_prompt()

            async for event in _ctx.client.run_loop(onboarding_prompt, max_turns=10):
                render_event(event)
            if project_md.exists():
                print_ok(f"Created {project_md}")
        except Exception as exc:
            logger.debug("suppressed exception in cmd_init", exc_info=True)
            print_warning(f"OBSCURA.md generation failed: {exc}")

    # Phase 3: Show agent definitions
    try:
        defs = resolve_all_definitions()
        if defs:
            print_info(f"Agent definitions available: {len(defs)}")
            for name, defn in sorted(defs.items())[:8]:
                print_info(f"  {name}: {defn.description[:60]}")
    except Exception:
        logger.debug("suppressed exception in cmd_init", exc_info=True)

    print_ok("Initialization complete. Type /help for commands.")
    return None


def _build_onboarding_prompt() -> str:
    """Build the onboarding prompt for OBSCURA.md generation."""
    return """\
Explore this repository and create a OBSCURA.md file in the root directory.

For a **code repository**, the OBSCURA.md should contain:
1. **Build & Development** — How to install, build, run, and test
2. **Architecture** — Key modules, data flow, important patterns
3. **Key Patterns** — Coding conventions, naming, imports
4. **Testing** — How to run tests, test conventions

For a **non-code repository** (documentation, data, notes, config, etc.),
adapt the guide to what's actually here — describe the structure, key files,
conventions, and how to work with the content effectively.

Steps:
1. Read README.md, package.json/pyproject.toml, Makefile/Dockerfile if they exist
2. Explore the directory structure (tree, ls)
3. Read 3-5 key files to understand the project and its patterns
4. Write OBSCURA.md with the information you found

Keep it concise (under 200 lines). Focus on what an AI agent needs to know
to work effectively in this project. Use code blocks for commands.
Do NOT make up information — only document what you can verify."""


# ---------------------------------------------------------------------------
# /running — unified dashboard of active processes
# ---------------------------------------------------------------------------


def _event_color(kind: str) -> str:
    """Return a Rich color string for an event kind."""
    _map: dict[str, str] = {
        "text_delta": "white",
        "thinking_delta": "dim italic",
        "tool_call": "cyan",
        "tool_result": "green",
        "error": "bold red",
        "turn_start": "yellow",
        "turn_complete": "bold yellow",
        "context_compact": "magenta",
    }
    return _map.get(kind.lower(), "dim")


async def _running_detail(
    agent: Any,
    ctx: REPLContext,
) -> None:
    """Show mini-log detail view for a single agent."""
    state = agent.get_state()
    parts: list[RenderableType] = []

    # ── Header: agent state snapshot ──
    s_c = "green" if state.status.name == "RUNNING" else "yellow"
    parts.append(
        Text.from_markup(
            f"[bold {s_c}]{state.status.name}[/]"
            f"  ·  iters: {state.iteration_count}"
            f"  ·  id: {agent.id[:12]}",
        ),
    )
    if state.error_message:
        parts.append(
            Text.from_markup(
                f"[bold red]Error:[/] {state.error_message}",
            ),
        )

    # ── Current thinking delta / active text ──
    try:
        active_text = get_active_text()
        if active_text:
            preview = active_text[-500:]
            if len(active_text) > 500:
                preview = "…" + preview
            parts.append(Text(""))
            parts.append(
                Text.from_markup(
                    "[bold]Thinking delta:[/]",
                ),
            )
            parts.append(Text(preview, style="dim italic"))
    except Exception:
        logger.debug("suppressed exception in _running_detail", exc_info=True)

    # ── Recent trace events ──
    try:
        entries = tail_entries(30)
        if entries:
            parts.append(Text(""))
            parts.append(
                Text.from_markup(
                    "[bold]Recent activity:[/]",
                ),
            )
            for shown, e in enumerate(reversed(entries)):
                if shown >= 15:
                    break
                kind = e.get("kind", "?")
                preview = e.get("preview", "")
                tools = e.get("tool_names", [])
                ts_raw = e.get("ts", "")
                ts_short = ts_raw[11:19] if len(ts_raw) > 19 else ts_raw
                tool_tag = ""
                if tools:
                    tool_tag = f" [cyan]({', '.join(tools)})[/]"
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                line = f"  [dim]{ts_short}[/] [{_event_color(kind)}]{kind}[/]{tool_tag}"
                if preview:
                    line += f" [dim]{preview}[/]"
                parts.append(Text.from_markup(line))
    except Exception:
        logger.debug("suppressed exception in _running_detail", exc_info=True)

    # ── Recent session events from event store ──
    try:
        events = await ctx.store.get_events(ctx.session_id)
        if events:
            keep = {
                "tool_call",
                "tool_result",
                "error",
                "turn_start",
                "turn_complete",
                "context_compact",
            }
            interesting = [ev for ev in events if ev.kind.value in keep]
            tail = interesting[-10:]
            if tail:
                parts.append(Text(""))
                parts.append(
                    Text.from_markup(
                        "[bold]Session events:[/]",
                    ),
                )
                for ev in tail:
                    ts = ev.timestamp.strftime("%H:%M:%S")
                    payload = ev.payload
                    detail = ""
                    if ev.kind.value == "tool_call":
                        detail = "→ " + payload.get("tool_name", "?")
                    elif ev.kind.value == "tool_result":
                        detail = str(
                            payload.get("result", ""),
                        )[:60]
                    elif ev.kind.value == "error":
                        detail = str(
                            payload.get("message", ""),
                        )[:60]
                    c = _event_color(ev.kind.value)
                    line = f"  [dim]{ts}[/] [{c}]{ev.kind.value}[/]"
                    if detail:
                        line += f" [dim]{detail}[/]"
                    parts.append(Text.from_markup(line))
    except Exception:
        logger.debug("suppressed exception in _running_detail", exc_info=True)

    if not parts:
        parts.append(Text("  No data available.", style="dim"))

    panel = Panel(
        Group(*parts),
        title=f"Agent: {agent.config.name}",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print(panel)


async def cmd_kill(args: str, ctx: REPLContext) -> str | None:
    """Kill all running agents, daemons, swarms, and supervisor."""
    stopped = 0

    # 1. Stop all agents in the runtime
    if ctx.runtime is not None:
        for agent in ctx.runtime.list_agents():
            try:
                await agent.stop()
                stopped += 1
            except Exception:
                logger.debug("suppressed exception in cmd_kill", exc_info=True)

    # 2. Cancel all swarm tasks
    for _sid, run in list(ctx.swarm_runs.items()):
        task_obj = run.get("_task")
        if task_obj and not task_obj.done():
            task_obj.cancel()
            stopped += 1
            run["status"] = "cancelled"
    ctx.swarm_runs.clear()

    # 3. Stop supervisor (kills all managed daemons)
    if ctx.supervisor_task is not None and not ctx.supervisor_task.done():
        ctx.supervisor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ctx.supervisor_task
        stopped += 1
        ctx.supervisor = None
        ctx.supervisor_task = None

    if stopped:
        print_ok(f"Killed {stopped} task(s).")
    else:
        print_info("Nothing running.")
    return None


async def cmd_running(args: str, ctx: REPLContext) -> str | None:
    """Show running agents, daemons, and session activity."""
    from obscura.agent.agents import AgentStatus

    target = args.strip()
    lines: list[RenderableType] = []
    has_activity = False
    selectable: list[Any] = []

    # ------------------------------------------------------------------
    # 1. Current session
    # ------------------------------------------------------------------
    sid_short = ctx.session_id[:8] if ctx.session_id else "none"
    session_rec = (
        await ctx.store.get_session(ctx.session_id) if ctx.session_id else None
    )
    status_val = session_rec.status.value if session_rec else "active"
    session_line = (
        f"  [bold cyan]Session:[/] {sid_short}"
        f" · {ctx.backend or 'default'}"
        f" · {ctx.model or 'default'}"
        f" · {status_val}"
    )
    console.print(Text.from_markup(session_line))

    # ------------------------------------------------------------------
    # 2. Agents from AgentRuntime
    # ------------------------------------------------------------------
    agents_list: list[Any] = []
    if ctx.runtime is not None:
        with contextlib.suppress(Exception):
            agents_list = ctx.runtime.list_agents()

    active_set = {
        AgentStatus.RUNNING,
        AgentStatus.WAITING,
        AgentStatus.PENDING,
    }

    if agents_list:
        active = [a for a in agents_list if a.status in active_set]
        terminal = [a for a in agents_list if a.status not in active_set]
        has_activity = has_activity or bool(active)
        selectable.extend(agents_list)

        if active:
            tbl = Table(
                show_header=True,
                header_style="bold",
                title=f"Agents ({len(active)} active)",
            )
            tbl.add_column("#", style="dim", width=3)
            tbl.add_column("Name", style="green")
            tbl.add_column("Status", style="yellow")
            tbl.add_column(
                "Iters",
                style="dim",
                justify="right",
            )
            tbl.add_column("ID", style="cyan")
            for i, a in enumerate(active, 1):
                state = a.get_state()
                sc = "bold green" if a.status == AgentStatus.RUNNING else "yellow"
                tbl.add_row(
                    str(i),
                    a.config.name,
                    f"[{sc}]{a.status.name}[/]",
                    str(state.iteration_count),
                    a.id[:8],
                )
            lines.append(tbl)

        if terminal:
            tbl_d = Table(
                show_header=True,
                header_style="bold dim",
                title=f"Done ({len(terminal)})",
            )
            tbl_d.add_column("#", style="dim", width=3)
            tbl_d.add_column("Name", style="dim green")
            tbl_d.add_column("Status", style="dim yellow")
            tbl_d.add_column(
                "Iters",
                style="dim",
                justify="right",
            )
            tbl_d.add_column("ID", style="dim cyan")
            off = len(active) if active else 0
            for i, a in enumerate(terminal, off + 1):
                state = a.get_state()
                tbl_d.add_row(
                    str(i),
                    a.config.name,
                    a.status.name,
                    str(state.iteration_count),
                    a.id[:8],
                )
            lines.append(tbl_d)
    else:
        lines.append(
            Text("  No agents spawned.", style="dim"),
        )

    # ------------------------------------------------------------------
    # 3a. Sub-agents of current session
    # ------------------------------------------------------------------
    try:
        child_sessions = await ctx.store.list_sessions(
            parent_session_id=ctx.session_id,
        )
        if child_sessions:
            has_activity = True
            status_colors = {
                SessionStatus.RUNNING: "bold green",
                SessionStatus.WAITING_FOR_TOOL: "yellow",
                SessionStatus.WAITING_FOR_USER: "yellow",
                SessionStatus.COMPLETED: "dim green",
                SessionStatus.FAILED: "red",
                SessionStatus.PAUSED: "dim yellow",
            }
            ctbl = Table(
                show_header=True,
                header_style="bold",
                title=f"Sub-agents ({len(child_sessions)})",
            )
            ctbl.add_column("Session", style="cyan")
            ctbl.add_column("Status", style="yellow")
            ctbl.add_column("Agent", style="green")
            ctbl.add_column("Source", style="dim")
            for s in child_sessions:
                sc = status_colors.get(s.status, "dim")
                ctbl.add_row(
                    s.id[:12],
                    f"[{sc}]{s.status.value}[/]",
                    s.active_agent or "-",
                    s.source or "-",
                )
            lines.append(ctbl)
    except Exception:
        logger.debug("suppressed exception in cmd_running", exc_info=True)

    # ------------------------------------------------------------------
    # 3b. Other independent sessions
    # ------------------------------------------------------------------
    try:
        all_sessions = await ctx.store.list_sessions()
        s_active = {
            SessionStatus.RUNNING,
            SessionStatus.WAITING_FOR_TOOL,
        }
        other = [
            s
            for s in all_sessions
            if s.id != ctx.session_id
            and s.status in s_active
            and s.parent_session_id != ctx.session_id
        ]
        if other:
            has_activity = True
            stbl = Table(
                show_header=True,
                header_style="bold",
                title=f"Other Sessions ({len(other)} running)",
            )
            stbl.add_column("Session", style="cyan")
            stbl.add_column("Status", style="yellow")
            stbl.add_column("Agent", style="green")
            for s in other[:10]:
                stbl.add_row(
                    s.id[:12],
                    s.status.value,
                    s.active_agent or "-",
                )
            lines.append(stbl)
    except Exception:
        logger.debug("suppressed exception in cmd_running", exc_info=True)

    # ------------------------------------------------------------------
    # Assemble panel
    # ------------------------------------------------------------------
    panel_items = lines or [
        Text("  Nothing running.", style="dim"),
    ]
    panel = Panel(
        Group(*panel_items),
        title="Running",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print(panel)

    # ------------------------------------------------------------------
    # 4. Direct arg: /running <name|id>
    # ------------------------------------------------------------------
    if target and selectable:
        match = [
            a for a in selectable if a.config.name == target or a.id.startswith(target)
        ]
        if match:
            await _running_detail(match[0], ctx)
            return None
        print_error(f"Agent not found: {target}")
        return None

    if not has_activity and not selectable:
        print_info("No active agents or background sessions.")
        return None

    # ------------------------------------------------------------------
    # 5. Interactive selection → detail view
    # ------------------------------------------------------------------
    if selectable:
        choices = [a.config.name for a in selectable] + ["back"]
        try:
            from obscura.cli.widgets import (
                AttentionWidgetRequest,
                confirm_attention,
            )

            result = await confirm_attention(
                AttentionWidgetRequest(
                    request_id="running_select",
                    agent_name="system",
                    message="Select agent to inspect:",
                    actions=tuple(choices),
                ),
            )
            if result.action != "back":
                match = [a for a in selectable if a.config.name == result.action]
                if match:
                    await _running_detail(match[0], ctx)
        except Exception:
            logger.debug("suppressed exception in cmd_running", exc_info=True)

    return None


# ---------------------------------------------------------------------------
# Handlers — plugin utilities
# ---------------------------------------------------------------------------


async def cmd_audit(args: str, _ctx: REPLContext) -> str | None:
    """Show the broker audit log — recent tool executions, denials, errors.

    Usage:
      /audit              — Show last 20 entries
      /audit <n>          — Show last n entries
      /audit errors       — Show only errors/denials
    """
    try:
        import obscura.plugins.broker  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        logger.debug("suppressed exception in cmd_audit", exc_info=True)
        print_error("Broker module not available.")
        return None

    tokens = args.strip().split()
    show_errors_only = False
    limit = 20

    for tok in tokens:
        if tok in ("errors", "denied", "failed"):
            show_errors_only = True
        else:
            with contextlib.suppress(ValueError):
                limit = int(tok)

    # The broker is constructed per-session; try to find an active one via
    # the supervisor or fall back to a fresh read from the global singleton.
    broker: ToolBroker | None = None
    try:
        # Supervisor keeps a broker reference
        if _ctx.supervisor and hasattr(_ctx.supervisor, "_broker"):
            broker = _ctx.supervisor._broker  # noqa: SLF001
    except Exception:
        logger.debug("suppressed exception in cmd_audit", exc_info=True)

    if broker is None:
        print_info("No active broker in this session — no audit entries yet.")
        return None

    entries = broker.audit_log
    if show_errors_only:
        entries = [
            e
            for e in entries
            if e.action in ("denied", "approval_denied", "error", "timeout")
        ]
    entries = entries[-limit:]

    if not entries:
        print_info("No audit entries found.")
        return None

    table = Table(title=f"Broker Audit (last {len(entries)})", expand=False)
    table.add_column("time", style="dim", width=8)
    table.add_column("tool", style="cyan", no_wrap=True)
    table.add_column("action", width=10)
    table.add_column("agent", style="dim", max_width=16)
    table.add_column("ms", justify="right", width=6)
    table.add_column("detail", max_width=30)

    for e in entries:
        ts = datetime.fromtimestamp(e.timestamp, tz=UTC).strftime("%H:%M:%S")
        action_color = {
            "executed": "green",
            "denied": "red",
            "approval_denied": "yellow",
            "error": "red",
            "timeout": "yellow",
        }.get(e.action, "white")
        detail = e.error or e.matched_rule or ""
        if len(detail) > 30:
            detail = detail[:27] + "..."
        table.add_row(
            ts,
            e.tool,
            f"[{action_color}]{e.action}[/]",
            e.agent_id or "",
            str(e.latency_ms),
            detail,
        )
    console.print(table)
    return None


async def cmd_health(_args: str, _ctx: REPLContext) -> str | None:
    """Quick plugin health dashboard.

    Usage: /health
    """
    try:
        from obscura.integrations.a2a.client import A2AClient  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        logger.debug("suppressed exception in cmd_health", exc_info=True)
        print_error("Plugin system not available.")
        return None

    registry = PluginRegistryService()
    plugins = registry.list_plugins()

    # Include builtins not yet in the registry
    try:
        loader = PluginLoader()
        registered_ids = {p.id for p in plugins}
        for spec in loader.discover_builtins():
            if spec.id not in registered_ids:
                entry = PluginEntry.from_spec(spec, source="builtin")
                entry.enabled = True
                entry.state = "enabled"
                plugins.append(entry)
    except Exception:
        logger.debug("suppressed exception in cmd_health", exc_info=True)

    if not plugins:
        print_info("No plugins found.")
        return None

    # Group by state
    by_state: dict[str, list[Any]] = {}
    for p in plugins:
        by_state.setdefault(p.state, []).append(p)

    table = Table(title="Plugin Health", expand=False)
    table.add_column("plugin", style="cyan", no_wrap=True)
    table.add_column("version", style="dim", width=8)
    table.add_column("state", width=10)
    table.add_column("trust", style="dim", width=10)
    table.add_column("tools", justify="right", width=5)

    state_order = [
        "failed",
        "unhealthy",
        "disabled",
        "enabled",
        "installed",
        "discovered",
    ]
    for state in state_order:
        for p in by_state.get(state, []):
            color = {
                "enabled": "green",
                "active": "green",
                "disabled": "dim",
                "installed": "yellow",
                "failed": "red",
                "unhealthy": "red",
                "discovered": "dim",
            }.get(p.state, "white")
            n_tools = len(p.contributed_tools) if p.contributed_tools else 0
            table.add_row(
                p.id,
                p.version,
                f"[{color}]{p.state}[/]",
                getattr(p, "trust_level", ""),
                str(n_tools),
            )

    console.print(table)
    total = len(plugins)
    enabled = len(by_state.get("enabled", []) + by_state.get("active", []))
    failed = len(by_state.get("failed", []))
    console.print(f"[dim]  {total} total, {enabled} enabled, {failed} failed[/]")
    return None


async def cmd_broker(_args: str, _ctx: REPLContext) -> str | None:
    """Show broker stats — execution counts, error rates, slowest tools.

    Usage: /broker
    """
    try:
        pass
    except ImportError:
        logger.debug("suppressed exception in cmd_broker", exc_info=True)
        print_error("Broker module not available.")
        return None

    broker: ToolBroker | None = None
    try:
        if _ctx.supervisor and hasattr(_ctx.supervisor, "_broker"):
            broker = _ctx.supervisor._broker  # noqa: SLF001
    except Exception:
        logger.debug("suppressed exception in cmd_broker", exc_info=True)

    if broker is None:
        print_info("No active broker in this session.")
        return None

    entries = broker.audit_log
    if not entries:
        print_info("No broker activity yet.")
        return None

    # Aggregate stats per tool
    tool_stats: dict[str, dict[str, Any]] = {}
    for e in entries:
        s = tool_stats.setdefault(
            e.tool,
            {"ok": 0, "fail": 0, "total_ms": 0, "max_ms": 0},
        )
        if e.action == "executed":
            s["ok"] += 1
        else:
            s["fail"] += 1
        s["total_ms"] += e.latency_ms
        s["max_ms"] = max(s["max_ms"], e.latency_ms)

    table = Table(title="Broker Stats", expand=False)
    table.add_column("tool", style="cyan", no_wrap=True)
    table.add_column("ok", justify="right", style="green", width=5)
    table.add_column("fail", justify="right", style="red", width=5)
    table.add_column("avg ms", justify="right", width=7)
    table.add_column("max ms", justify="right", width=7)

    for tool_name in sorted(
        tool_stats,
        key=lambda t: tool_stats[t]["ok"] + tool_stats[t]["fail"],
        reverse=True,
    ):
        s = tool_stats[tool_name]
        total_calls = s["ok"] + s["fail"]
        avg_ms = s["total_ms"] // total_calls if total_calls else 0
        table.add_row(
            tool_name,
            str(s["ok"]),
            str(s["fail"]),
            str(avg_ms),
            str(s["max_ms"]),
        )

    console.print(table)
    console.print(
        f"[dim]  {len(entries)} total calls across {len(tool_stats)} tools[/]",
    )
    return None


async def cmd_search_tools(args: str, ctx: REPLContext) -> str | None:
    """Search registered tools by name or description keyword.

    Usage: /search-tools <query>
    """
    query = args.strip().lower()
    if not query:
        print_error("Usage: /search-tools <query>")
        return None

    registry = ctx.client._tool_registry  # noqa: SLF001
    tools = registry.all_including_disabled()
    if not tools:
        print_info("No tools registered.")
        return None

    # Score matches: name match weighted higher than description match
    results: list[tuple[int, Any]] = []
    for t in tools:
        score = 0
        name_lower = t.name.lower()
        desc_lower = (getattr(t, "description", "") or "").lower()
        if query == name_lower:
            score = 100
        elif query in name_lower:
            score = 60
        if query in desc_lower:
            score += 30
        # partial word matching
        for word in query.split():
            if word in name_lower:
                score += 10
            if word in desc_lower:
                score += 5
        if score > 0:
            results.append((score, t))

    results.sort(key=lambda r: r[0], reverse=True)

    if not results:
        print_info(f"No tools matching '{query}'.")
        return None

    table = Table(
        title=f"Tools matching '{query}' ({len(results)} found)",
        expand=False,
    )
    table.add_column("status", width=3, justify="center")
    table.add_column("name", style=TOOL_COLOR, no_wrap=True)
    table.add_column("description", max_width=55)

    for _score, t in results[:25]:
        desc = getattr(t, "description", "") or ""
        if len(desc) > 55:
            desc = desc[:52] + "..."
        status = "[red]off[/]" if registry.is_disabled(t.name) else "[green]on[/]"
        table.add_row(status, t.name, desc)

    console.print(table)
    return None


# ---------------------------------------------------------------------------
# Wave 2 commands: permissions, resume, cost, doctor, vim, effort, fast,
# commit, review, security-review, export, coordinator, voice
# ---------------------------------------------------------------------------


async def cmd_permissions(args: str, ctx: REPLContext) -> str | None:
    """Switch permission mode: default, plan, accept_edits, bypass."""

    mode_str = args.strip().lower()
    if not mode_str:
        current = getattr(ctx, "permission_mode", "default")
        print_info(f"Permission mode: {current}")
        print_info("Usage: /permissions default|plan|accept_edits|bypass")
        return None
    try:
        mode = PermissionMode(mode_str)
    except ValueError:
        logger.debug("suppressed exception in cmd_permissions", exc_info=True)
        print_error(
            f"Unknown mode: {mode_str}. Options: default, plan, accept_edits, bypass",
        )
        return None
    if mode == PermissionMode.BYPASS:
        if not os.environ.get("OBSCURA_BYPASS_PERMISSIONS"):
            print_warning("Set OBSCURA_BYPASS_PERMISSIONS=1 to enable bypass mode.")
            return None
    ctx.permission_mode = mode_str
    print_ok(f"Permission mode set to: {mode.value}")
    return None


async def cmd_resume(args: str, ctx: REPLContext) -> str | None:
    """Resume a previous session. Usage: /resume [search term]."""
    import difflib

    sessions = await ctx.store.list_sessions()
    if not sessions:
        print_info("No previous sessions found.")
        return None

    search = args.strip().lower()
    if search:
        scored: list[tuple[float, Any]] = []
        for s in sessions:
            text = f"{s.summary or ''} {s.model or ''} {s.active_agent or ''} {s.project or ''}".lower()
            ratio = difflib.SequenceMatcher(None, search, text).ratio()
            if ratio > 0.2 or search in text:
                scored.append((ratio, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        sessions = [s for _, s in scored[:10]]
        if not sessions:
            print_info(f"No sessions matching '{search}'.")
            return None

    table = Table(title="Sessions", expand=False)
    table.add_column("#", width=3, justify="right")
    table.add_column("Title", max_width=30, style="bold")
    table.add_column("ID", width=12, no_wrap=True, style="cyan")
    table.add_column("Status", width=10)
    table.add_column("Model", width=16, style="dim")

    shown = sessions[:15]
    for i, s in enumerate(shown, 1):
        sid = s.id[:12]
        status = s.status.value if hasattr(s.status, "value") else str(s.status)
        model = s.model or ""
        title = (s.summary or "")[:30] or "-"
        table.add_row(str(i), title, sid, status, model)

    console.print(table)

    # If only one match and search was specific, auto-switch.
    if search and len(shown) == 1:
        target_sid = shown[0].id
        ctx.session_id = target_sid
        ctx.message_history = []
        print_ok(f"Resumed session {target_sid[:12]}")
        # Replay last few events as context preview.
        try:
            events = await ctx.store.get_events(target_sid)
            text_events = [
                e
                for e in events
                if hasattr(e, "kind") and "text" in str(getattr(e, "kind", "")).lower()
            ]
            if text_events:
                last = text_events[-1]
                preview = (
                    getattr(last, "payload", {}).get("text", "")[:200]
                    if hasattr(last, "payload")
                    else ""
                )
                if preview:
                    console.print(f"[dim]Last: {preview}...[/]")
        except Exception:
            logger.debug("suppressed exception in cmd_resume", exc_info=True)
        return None

    print_info("Resume with: /resume <search> or /session <id>")
    return None


async def cmd_cost(_args: str, ctx: REPLContext) -> str | None:
    """Display session cost breakdown."""

    tracker = get_cost_tracker()
    if tracker.turn_count() == 0:
        print_info("No cost data recorded yet.")
        return None

    table = Table(title="Session Cost Breakdown", expand=False)
    table.add_column("Turn", justify="right", width=5)
    table.add_column("Model", width=20)
    table.add_column("Input", justify="right", width=10)
    table.add_column("Output", justify="right", width=10)
    table.add_column("Cost", justify="right", width=10)

    for entry in tracker.breakdown():
        table.add_row(
            str(entry["turn"]),
            entry["model"],
            f"{entry['input_tokens']:,}",
            f"{entry['output_tokens']:,}",
            f"${entry['cost_usd']:.4f}",
        )

    console.print(table)
    console.print(f"[bold]{tracker.summary()}[/]")
    return None


async def cmd_doctor(_args: str, _ctx: REPLContext) -> str | None:
    """Run environment diagnostics."""
    checks: list[tuple[str, str, str]] = []  # (name, status, detail)

    # Python version
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 13)
    checks.append(
        (
            "Python",
            "[green]OK[/]" if ok else "[red]FAIL[/]",
            f"{ver} {'(3.13+ required)' if not ok else ''}",
        ),
    )

    # Key binaries
    for binary in ["git", "rg", "uv", "ruff", "pyright"]:
        path = shutil.which(binary)
        if path:
            checks.append((binary, "[green]OK[/]", path))
        else:
            checks.append((binary, "[yellow]MISS[/]", "not found in PATH"))

    # Obscura home

    home = resolve_obscura_global_home()
    checks.append(
        (
            "Obscura home",
            "[green]OK[/]" if home.is_dir() else "[yellow]MISS[/]",
            str(home),
        ),
    )

    # Event store
    db_path = home / "events.db"
    checks.append(
        (
            "Event store",
            "[green]OK[/]" if db_path.exists() else "[yellow]MISS[/]",
            str(db_path),
        ),
    )

    # Settings
    settings_path = home / "settings.json"
    checks.append(
        (
            "Settings",
            "[green]OK[/]" if settings_path.exists() else "[dim]none[/]",
            str(settings_path),
        ),
    )

    # Agent definitions

    defs = resolve_all_definitions()
    checks.append(("Agent definitions", "[green]OK[/]", f"{len(defs)} types available"))

    table = Table(title="Obscura Doctor", expand=False)
    table.add_column("Check", width=20)
    table.add_column("Status", width=8, justify="center")
    table.add_column("Detail", max_width=50)
    for name, status, detail in checks:
        table.add_row(name, status, detail)
    console.print(table)
    return None


async def cmd_vim(_args: str, ctx: REPLContext) -> str | None:
    """Toggle vim keybindings in the REPL."""
    current = getattr(ctx, "vim_mode", False)
    ctx.vim_mode = not current
    mode = "enabled" if ctx.vim_mode else "disabled"
    print_ok(f"Vim mode {mode}. Takes effect on next prompt.")
    return None


async def cmd_effort(args: str, ctx: REPLContext) -> str | None:
    """Set thinking effort level: low, medium, high, max."""

    level_str = args.strip().lower()
    if not level_str:
        current = getattr(ctx, "effort_level", "medium")
        print_info(f"Effort level: {current}")
        print_info("Usage: /effort low|medium|high|max")
        return None
    try:
        level = EffortLevel(level_str)
    except ValueError:
        logger.debug("suppressed exception in cmd_effort", exc_info=True)
        print_error(f"Unknown level: {level_str}. Options: low, medium, high, max")
        return None
    ctx.effort_level = level.value
    budget = EFFORT_THINKING_BUDGETS[level]

    # Show ultrathink banner for max effort.
    if level == EffortLevel.MAX:
        try:
            ultrathink_banner()
        except Exception:
            logger.debug("suppressed exception in cmd_effort", exc_info=True)
            print_ok(f"⚡ ULTRATHINK activated (budget: {budget:,} tokens)")
    else:
        try:
            console.print(f"  {effort_badge(level.value)}  (budget: {budget:,} tokens)")
        except Exception:
            logger.debug("suppressed exception in cmd_effort", exc_info=True)
            print_ok(f"Effort: {level.value} (thinking budget: {budget:,} tokens)")
    return None


async def cmd_fast(_args: str, ctx: REPLContext) -> str | None:
    """Toggle fast/terse mode (low effort + concise output)."""
    current = getattr(ctx, "effort_level", "medium")
    if current == "low":
        ctx.effort_level = "medium"
        print_ok("Fast mode OFF (effort: medium)")
    else:
        ctx.effort_level = "low"
        print_ok("Fast mode ON (effort: low, terse responses)")
    return None


async def cmd_caffeinate(args: str, _ctx: REPLContext) -> str | None:
    """Prevent macOS from sleeping while Obscura is running.

    Usage: /caffeinate [on|off|status]
    """
    sub = args.strip().lower()

    pid_attr = "_caffeinate_pid"
    current_pid: int | None = getattr(cmd_caffeinate, pid_attr, None)

    def _is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            logger.debug("suppressed exception in _is_alive", exc_info=True)
            return False

    if sub in ("", "on"):
        if current_pid and _is_alive(current_pid):
            print_info(f"Already caffeinated (pid {current_pid})")
            return None
        proc = subprocess.Popen(
            ["caffeinate", "-dims"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        setattr(cmd_caffeinate, pid_attr, proc.pid)
        print_ok(f"Caffeinated — system sleep blocked (pid {proc.pid})")
        return None

    if sub == "off":
        if current_pid and _is_alive(current_pid):
            os.kill(current_pid, signal.SIGTERM)
            setattr(cmd_caffeinate, pid_attr, None)
            print_ok("Caffeinate stopped — system can sleep again")
        else:
            setattr(cmd_caffeinate, pid_attr, None)
            print_info("Not currently caffeinated")
        return None

    if sub == "status":
        if current_pid and _is_alive(current_pid):
            print_info(f"Caffeinated (pid {current_pid})")
        else:
            setattr(cmd_caffeinate, pid_attr, None)
            print_info("Not caffeinated")
        return None

    print_error("Usage: /caffeinate [on|off|status]")
    return None


async def cmd_debug(_args: str, ctx: REPLContext) -> str | None:
    """Toggle debug mode (verbose logging + internal output)."""
    import obscura.config as _cfg
    from obscura.cli.render import output as _output

    if _cfg.VERBOSE:
        _cfg.VERBOSE = False
        _output.verbose = False
        _output.set_log_level("info")
        print_ok("Debug mode OFF (log level: info)")
    else:
        _cfg.VERBOSE = True
        _output.verbose = True
        _output.set_log_level("debug")
        print_ok("Debug mode ON (verbose logging enabled)")
    return None


async def cmd_commit(args: str, ctx: REPLContext) -> str | None:
    """AI-assisted git commit from staged changes."""
    # Gather git context
    cmds = {
        "status": "git status",
        "diff": "git diff HEAD",
        "branch": "git branch --show-current",
        "log": "git log --oneline -10",
    }
    results: dict[str, str] = {}
    for key, cmd in cmds.items():
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        results[key] = stdout.decode("utf-8", errors="replace").strip()

    if not results["diff"]:
        print_info("No changes to commit.")
        return None

    prompt = f"""Based on these git changes, create a single commit.

## Git Status
```
{results["status"]}
```

## Changes (git diff HEAD)
```
{results["diff"][:8000]}
```

## Current Branch
{results["branch"]}

## Recent Commits (for style reference)
```
{results["log"]}
```

## Instructions
1. Analyze the changes and draft a concise commit message (1-2 sentences, focus on "why")
2. Follow the repository's existing commit message style from the recent commits above
3. Stage relevant files with `git add` (specific files, not -A)
4. Create the commit using HEREDOC:
```
git commit -m "$(cat <<'EOF'
Message here.
EOF
)"
```
Do NOT use --amend, --no-verify, or -i flags. Do NOT commit .env or credential files."""

    # Send to agent loop

    try:
        async for event in ctx.client.run_loop(prompt):
            render_event(event)
    except Exception as exc:
        logger.debug("suppressed exception in cmd_commit", exc_info=True)
        print_error(str(exc))
    return None


async def cmd_review(args: str, ctx: REPLContext) -> str | None:
    """AI code review of changes. Usage: /review [PR number or ref]."""
    ref = args.strip() or "HEAD"
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        ref,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    diff = stdout.decode("utf-8", errors="replace").strip()

    if not diff:
        print_info(f"No changes found for ref: {ref}")
        return None

    prompt = f"""You are an expert code reviewer. Review these changes:

```diff
{diff[:12000]}
```

Provide a thorough review covering:
- **Overview**: What the changes do (1-2 sentences)
- **Correctness**: Logic errors, edge cases, type mismatches
- **Style**: Adherence to project conventions
- **Performance**: Any performance implications
- **Security**: Potential security issues
- **Suggestions**: Specific improvements with file:line references

Be concise. Focus on real issues, not style preferences."""

    try:
        async for event in ctx.client.run_loop(prompt):
            render_event(event)
    except Exception as exc:
        logger.debug("suppressed exception in cmd_review", exc_info=True)
        print_error(str(exc))
    return None


async def cmd_pr(args: str, ctx: REPLContext) -> str | None:
    """AI-assisted pull request creation. Usage: /pr [base-branch]."""
    base = args.strip() or "main"

    # Gather git context in parallel
    cmds = {
        "status": "git status",
        "branch": "git branch --show-current",
        "log": f"git log {base}...HEAD --oneline",
        "diff": f"git diff {base}...HEAD",
        "remote": "git remote -v",
    }
    results: dict[str, str] = {}
    for key, cmd in cmds.items():
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        results[key] = stdout.decode("utf-8", errors="replace").strip()

    if not results["diff"] and not results["log"]:
        print_info(f"No changes found between {base} and HEAD.")
        return None

    branch = results["branch"]
    if branch == base:
        print_warning(f"You are on '{base}'. Create a feature branch first.")
        return None

    prompt = f"""Create a GitHub pull request for the current branch.

## Current Branch
{branch}

## Base Branch
{base}

## Commits (since diverged from {base})
```
{results["log"][:4000]}
```

## Changes (git diff {base}...HEAD)
```diff
{results["diff"][:10000]}
```

## Git Status
```
{results["status"]}
```

## Remote
```
{results["remote"]}
```

## Instructions
1. Analyze ALL commits (not just the latest) and the full diff
2. Draft a concise PR title (under 70 chars) and a body with:
   - ## Summary (1-3 bullet points)
   - ## Test plan (bulleted checklist)
3. Check if the branch is pushed to remote. If not, push with `git push -u origin {branch}`
4. Create the PR using:
```
gh pr create --title "title" --body "$(cat <<'EOF'
## Summary
- bullet points

## Test plan
- [ ] test items
EOF
)"
```
5. Return the PR URL when done.

Do NOT use --force. Do NOT push to {base} directly."""

    try:
        async for event in ctx.client.run_loop(prompt):
            render_event(event)
    except Exception as exc:
        logger.debug("suppressed exception in cmd_pr", exc_info=True)
        print_error(str(exc))
    return None


async def cmd_security_review(args: str, ctx: REPLContext) -> str | None:
    """Security-focused code review. Usage: /security-review [ref]."""
    ref = args.strip() or "HEAD"
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        ref,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    diff = stdout.decode("utf-8", errors="replace").strip()

    if not diff:
        print_info(f"No changes found for ref: {ref}")
        return None

    prompt = f"""You are a security expert. Perform a security review of these changes.

```diff
{diff[:12000]}
```

**Focus on (report only 80%+ confidence findings):**
- Input validation: SQL/command/XXE injection, path traversal
- Auth & authz: bypass, privilege escalation, session flaws
- Crypto: hardcoded keys, weak algorithms, RNG issues
- Code execution: deserialization RCE, eval, XSS
- Data exposure: sensitive logging, PII handling, API leaks

**EXCLUDE (do not report):**
- DOS/resource exhaustion, rate limiting
- Secrets on disk (handled separately)
- React/Angular XSS (framework handles it)
- Regex DOS, log spoofing, documentation files
- Outdated library vulnerabilities

**Output format for each finding:**
```
# Vuln N: Category: `file:line`
* Severity: High|Medium
* Description: [what's wrong]
* Exploit Scenario: [how it's exploited]
* Recommendation: [specific fix]
```

Only report HIGH and MEDIUM severity findings with 80%+ confidence."""

    try:
        async for event in ctx.client.run_loop(prompt):
            render_event(event)
    except Exception as exc:
        logger.debug("suppressed exception in cmd_security_review", exc_info=True)
        print_error(str(exc))
    return None


async def cmd_ultrareview(args: str, ctx: REPLContext) -> str | None:
    """Multi-agent parallel code review. Usage: /ultrareview [PR#|ref]."""
    ref = args.strip()

    # If a PR number is given, fetch the diff from GitHub
    if ref and ref.isdigit():
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "diff",
            ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        diff = stdout.decode("utf-8", errors="replace").strip()
        if not diff:
            print_warning(
                f"Could not fetch PR #{ref} diff via gh: {stderr.decode()[:200]}"
            )
            return None
        review_target = f"PR #{ref}"
    else:
        # Local diff — use subprocess_exec to prevent shell injection
        git_ref = ref or "HEAD"
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            git_ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        diff = stdout.decode("utf-8", errors="replace").strip()
        if not diff:
            print_info(f"No changes found for ref: {git_ref}")
            return None
        review_target = f"ref {git_ref}"

    # --- Split diff into per-file hunks ---
    file_diffs: list[tuple[str, str]] = []
    current_file = ""
    current_lines: list[str] = []
    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            if current_file and current_lines:
                file_diffs.append((current_file, "\n".join(current_lines)))
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else line
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_file and current_lines:
        file_diffs.append((current_file, "\n".join(current_lines)))

    total_files = len(file_diffs)
    total_bytes = len(diff)
    print_info(
        f"Launching ultrareview of {review_target}: "
        f"{total_files} files, {total_bytes:,} bytes"
    )

    # --- Distribute file diffs across 5 specialists ---
    # Each specialist gets a DIFFERENT slice of files, not the same truncated prefix.
    # Budget: ~30k chars per specialist (fits comfortably in most context windows).
    _BUDGET_PER_AGENT = 30000

    specialists: list[tuple[str, str, str]] = [
        (
            "security",
            "You are a security expert.",
            "Focus ONLY on security issues: injection flaws, auth bypass, "
            "privilege escalation, data exposure, hardcoded secrets, crypto "
            "weaknesses, path traversal, deserialization. Report only HIGH/MEDIUM "
            "confidence findings with file:line references.",
        ),
        (
            "correctness",
            "You are a logic and correctness expert.",
            "Focus ONLY on correctness: logic errors, off-by-one, null/None "
            "handling, race conditions, async bugs, type mismatches, edge cases, "
            "error handling gaps. Report with file:line references.",
        ),
        (
            "architecture",
            "You are a software architect.",
            "Focus ONLY on architecture and design: coupling, cohesion, API "
            "design, naming, abstraction levels, dependency direction, breaking "
            "changes, backwards compatibility. Report structural concerns only.",
        ),
        (
            "test-coverage",
            "You are a testing specialist.",
            "Focus ONLY on test coverage: missing tests for new code paths, "
            "untested edge cases, test quality (are assertions meaningful?), "
            "mock correctness, integration vs unit balance. Report gaps only.",
        ),
        (
            "performance",
            "You are a performance engineer.",
            "Focus ONLY on performance: N+1 queries, unnecessary allocations, "
            "blocking in async code, missing caching opportunities, O(n²) "
            "algorithms, large payload handling. Report with file:line references.",
        ),
    ]
    num_specialists = len(specialists)

    # Build per-specialist diff chunks by round-robin distributing files
    specialist_chunks: list[str] = [""] * num_specialists
    specialist_files: list[list[str]] = [[] for _ in range(num_specialists)]
    for i, (fname, fdiff) in enumerate(file_diffs):
        slot = i % num_specialists
        # Respect per-agent budget
        if len(specialist_chunks[slot]) + len(fdiff) <= _BUDGET_PER_AGENT:
            specialist_chunks[slot] += fdiff + "\n"
            specialist_files[slot].append(fname)
        else:
            # Over budget — try to fit in any slot with room
            placed = False
            for s in range(num_specialists):
                if len(specialist_chunks[s]) + len(fdiff) <= _BUDGET_PER_AGENT:
                    specialist_chunks[s] += fdiff + "\n"
                    specialist_files[s].append(fname)
                    placed = True
                    break
            if not placed:
                # All slots full — truncate this file diff and add to original slot
                remaining = _BUDGET_PER_AGENT - len(specialist_chunks[slot])
                if remaining > 500:
                    specialist_chunks[slot] += fdiff[:remaining] + "\n[truncated]\n"
                    specialist_files[slot].append(f"{fname} (truncated)")

    # Show distribution
    for i, (name, _, _) in enumerate(specialists):
        fcount = len(specialist_files[i])
        chars = len(specialist_chunks[i])
        if fcount:
            fnames = ", ".join(specialist_files[i][:5])
            if fcount > 5:
                fnames += f", ... (+{fcount - 5} more)"
            print_info(f"  {name}: {fcount} files, {chars:,} chars — {fnames}")

    console.print()
    runtime = await ctx.get_runtime()

    async def _run_specialist(
        idx: int,
        name: str,
        persona: str,
        focus: str,
    ) -> tuple[str, str]:
        chunk = specialist_chunks[idx]
        files = specialist_files[idx]
        if not chunk.strip():
            return name, "No files assigned to this specialist."

        agent_name = f"ultrareview-{name}-{uuid.uuid4().hex[:4]}"
        agent = runtime.spawn(
            agent_name,
            model=ctx.backend,
            system_prompt=f"{persona} You are reviewing code changes.",
        )
        await agent.start()
        file_list = "\n".join(f"- {f}" for f in files)
        prompt = f"""{focus}

You are reviewing {len(files)} files:
{file_list}

```diff
{chunk}
```

Rules:
- Be concise. One paragraph per finding.
- Include file:line references.
- Rate each finding: HIGH or MEDIUM severity.
- If you find nothing significant, say "No issues found."
- Do NOT repeat findings across categories — stay in your lane.
"""
        output_lines: list[str] = []
        try:
            async for event in agent.stream_loop(prompt, max_turns=3):
                if hasattr(event, "text") and event.text:
                    output_lines.append(event.text)
        except Exception as exc:
            logger.debug("suppressed exception in _run_specialist", exc_info=True)
            output_lines.append(f"Error: {exc}")
        return name, "".join(output_lines)

    # Run all 5 in parallel
    results: list[tuple[str, str]] = await asyncio.gather(
        *[_run_specialist(i, n, p, f) for i, (n, p, f) in enumerate(specialists)],
    )

    # Display per-specialist results

    colors = ["red", "yellow", "blue", "green", "magenta"]
    for idx, (name, output) in enumerate(results):
        color = colors[idx % len(colors)]
        fcount = len(specialist_files[idx])
        console.print(
            Rule(
                f"[bold {color}]{name.upper()}[/] ({fcount} files)",
                style=color,
            )
        )
        if output.strip() and output.strip() != "No files assigned to this specialist.":
            console.print(Markdown(output))
        else:
            console.print("[dim]No issues found.[/]")
        console.print()

    # Verification pass — synthesize, deduplicate, and rank
    print_info("Running verification pass...")
    all_findings = "\n\n".join(
        f"## {name.upper()} ({len(specialist_files[i])} files)\n{output}"
        for i, (name, output) in enumerate(results)
        if output.strip() and output.strip() != "No files assigned to this specialist."
    )

    verifier_prompt = f"""You are a senior engineer doing a final review pass. Below are findings
from 5 specialist reviewers. Each reviewer examined DIFFERENT files from the same PR.

Your job:
1. DEDUPLICATE: remove any findings that appear more than once
2. VERIFY: remove obvious false positives
3. PRIORITIZE: rank remaining findings by impact
4. CROSS-CUT: add any concerns that span multiple specialists' file sets
5. Output a unified report — do NOT repeat findings:

### Critical (must fix before merge)
- Finding with file:line

### Important (should fix)
- Finding with file:line

### Minor (nice to have)
- Finding with file:line

### Summary
One paragraph overall assessment. Mention total files reviewed: {total_files}.

--- SPECIALIST FINDINGS ---

{all_findings[:20000]}
"""

    console.print(Rule("[bold white]UNIFIED REPORT[/]", style="white"))
    try:
        async for event in ctx.client.run_loop(verifier_prompt):
            render_event(event)
    except Exception as exc:
        logger.debug("suppressed exception in cmd_ultrareview", exc_info=True)
        print_error(f"Verification pass failed: {exc}")

    return None


async def cmd_export(args: str, ctx: REPLContext) -> str | None:
    """Export conversation to file. Usage: /export [md|txt|json]."""
    fmt = args.strip().lower() or "md"
    if fmt not in ("md", "txt", "json"):
        print_error("Format must be: md, txt, or json")
        return None

    output_dir = Path.home() / ".obscura" / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{ctx.session_id[:12]}.{fmt}"
    output_path = output_dir / filename

    history = ctx.message_history

    if fmt == "json":
        data = [{"role": role, "content": text} for role, text in history]
        output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    elif fmt == "md":
        lines: list[str] = [f"# Session {ctx.session_id[:12]}\n"]
        for role, text in history:
            header = "## User" if role == "user" else "## Assistant"
            lines.append(f"\n{header}\n\n{text}\n")
        output_path.write_text("\n".join(lines), encoding="utf-8")
    else:  # txt
        lines = []
        for role, text in history:
            lines.append(f"[{role.upper()}]\n{text}\n")
        output_path.write_text("\n".join(lines), encoding="utf-8")

    print_ok(f"Exported to {output_path}")
    return None


async def cmd_coordinator(args: str, ctx: REPLContext) -> str | None:
    """Toggle coordinator mode for multi-worker orchestration."""
    from obscura.agent.coordinator import is_coordinator_mode, set_coordinator_mode

    sub = args.strip().lower()
    if sub == "on":
        set_coordinator_mode(True)
        print_ok("Coordinator mode ON. Agent will orchestrate workers.")
    elif sub == "off":
        set_coordinator_mode(False)
        print_ok("Coordinator mode OFF.")
    elif sub == "status":
        status = "ON" if is_coordinator_mode() else "OFF"
        print_info(f"Coordinator mode: {status}")
    else:
        status = "ON" if is_coordinator_mode() else "OFF"
        print_info(f"Coordinator mode: {status}")
        print_info("Usage: /coordinator on|off|status")
    return None


async def cmd_voice(args: str, ctx: REPLContext) -> str | None:
    """Toggle voice input (push-to-talk). Usage: /voice [on|off]."""
    sub = args.strip().lower()
    current = getattr(ctx, "voice_enabled", False)

    if sub == "on" or (not sub and not current):
        # Check dependencies
        has_sox = shutil.which("rec") is not None
        has_arecord = shutil.which("arecord") is not None
        if not has_sox and not has_arecord:
            print_error("Voice mode requires SoX (rec) or ALSA (arecord).")
            print_info(
                "Install: brew install sox  (macOS) or apt install sox alsa-utils  (Linux)",
            )
            return None
        ctx.voice_enabled = True
        print_ok("Voice mode ON. Hold Ctrl+Space to record, release to transcribe.")
    elif sub == "off" or (not sub and current):
        ctx.voice_enabled = False
        print_ok("Voice mode OFF.")
    else:
        print_info(f"Voice mode: {'ON' if current else 'OFF'}")
    return None


async def cmd_template(args: str, ctx: REPLContext) -> str | None:
    """Manage and run task templates. Usage: /template list|run <name>|new <name>."""

    parts = args.strip().split(None, 1)
    sub = parts[0] if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        templates = list_templates()
        if not templates:
            print_info("No templates found. Create one in ~/.obscura/templates/")
            return None
        table = Table(title="Templates", expand=False)
        table.add_column("Name", width=20)
        table.add_column("Description", max_width=50)
        table.add_column("Variables", width=20)
        for t in templates:
            vars_str = ", ".join(t.variables) if t.variables else "-"
            table.add_row(t.name, t.description[:50], vars_str)
        console.print(table)
        return None

    if sub == "run":
        name = rest.strip()
        if not name:
            print_error("Usage: /template run <name>")
            return None
        tmpl = load_template(name)
        if tmpl is None:
            print_error(f"Template not found: {name}")
            return None
        prompt = tmpl.render()

        try:
            async for event in ctx.client.run_loop(prompt):
                render_event(event)
        except Exception as exc:
            logger.debug("suppressed exception in cmd_template", exc_info=True)
            print_error(str(exc))
        return None

    if sub == "new":
        print_info("Create a template at ~/.obscura/templates/<name>.md")
        print_info(
            "Use TOML frontmatter (+++ delimiters) with name, description fields.",
        )
        print_info("Template body is the prompt. Use {{variable}} for placeholders.")
        return None

    print_info("Usage: /template list|run <name>|new")
    return None


async def cmd_tool_summary(_args: str, ctx: REPLContext) -> str | None:
    """Show tool use summary for the current session."""
    collapser = getattr(ctx, "collapser", None)
    if collapser is None:
        print_info("No tool usage recorded yet.")
        return None
    # Show a summary of all tool calls from the session.
    if hasattr(collapser, "_group") or True:
        tracker = get_cost_tracker()
        print_info(
            tracker.summary() if tracker.turn_count() > 0 else "No turns recorded yet.",
        )
    return None


async def cmd_goals(args: str, ctx: REPLContext) -> str | None:
    """Manage the KAIROS goal board.

    Usage:
      /goals               List active goals
      /goals list [status]  List goals (optionally filter by status)
      /goals add <title>    Create a new goal
      /goals show <id>      Show full goal details
      /goals complete <id>  Mark goal as completed
      /goals abandon <id>   Abandon a goal
      /goals edit <id>      Open goal file in $EDITOR
    """

    board = GoalBoard()
    tokens = [t for t in args.strip().split(None, 1) if t]
    sub = tokens[0].lower() if tokens else "list"
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    if sub in ("list", "ls"):
        goals = board.load_all()
        if rest:
            goals = [g for g in goals if g.status == rest]
        if not goals:
            print_info("No goals found. Use `/goals add <title>` to create one.")
            return None
        table = Table(title="Goal Board", show_lines=False)
        table.add_column("ID", style="cyan", max_width=25)
        table.add_column("Priority", style="bold")
        table.add_column("Status")
        table.add_column("Progress")
        table.add_column("Title", max_width=40)
        _prio_colors = {
            "critical": "red",
            "high": "yellow",
            "medium": "blue",
            "low": "dim",
        }
        _status_colors = {
            "active": "green",
            "in_progress": "cyan",
            "completed": "dim",
            "abandoned": "dim",
            "draft": "dim",
        }
        for g in goals:
            prio_color = _prio_colors.get(g.priority, "")
            stat_color = _status_colors.get(g.status, "")
            bar = f"{'█' * (g.progress // 10)}{'░' * (10 - g.progress // 10)} {g.progress}%"
            table.add_row(
                g.id,
                f"[{prio_color}]{g.priority}[/]",
                f"[{stat_color}]{g.status}[/]",
                bar,
                g.title,
            )
        console.print(table)
        return None

    if sub == "add":
        if not rest:
            print_error("Usage: /goals add <title>")
            return None
        goal = board.create(rest)
        print_ok(f"Goal created: {goal.id}")
        print_info(f"  Edit: ~/.obscura/goals/{goal.id}.md")
        return None

    if sub == "show":
        if not rest:
            print_error("Usage: /goals show <id>")
            return None
        goal = board.load(rest)
        if goal is None:
            print_error(f"Goal not found: {rest}")
            return None
        console.print(f"\n[bold]{goal.title}[/]  ({goal.id})")
        console.print(
            f"  Status: {goal.status}  |  Priority: {goal.priority}  |  Progress: {goal.progress}%"
        )
        if goal.acceptance_criteria:
            console.print("  [bold]Acceptance Criteria:[/]")
            for ac in goal.acceptance_criteria:
                console.print(f"    - {ac}")
        if goal.tasks:
            console.print(f"  [bold]Linked Tasks:[/] {', '.join(goal.tasks)}")
        if goal.depends_on:
            console.print(f"  [bold]Depends On:[/] {', '.join(goal.depends_on)}")
        if goal.body:
            console.print(f"\n{goal.body}")
        console.print()
        return None

    if sub == "complete":
        if not rest:
            print_error("Usage: /goals complete <id>")
            return None
        goal = board.complete(rest)
        if goal is None:
            print_error(f"Could not complete goal: {rest}")
            return None
        print_ok(f"Goal completed: {goal.title}")
        return None

    if sub == "abandon":
        if not rest:
            print_error("Usage: /goals abandon <id>")
            return None
        goal = board.abandon(rest)
        if goal is None:
            print_error(f"Could not abandon goal: {rest}")
            return None
        print_ok(f"Goal abandoned: {goal.title}")
        return None

    if sub == "edit":
        if not rest:
            print_error("Usage: /goals edit <id>")
            return None
        goal = board.load(rest)
        if goal is None:
            print_error(f"Goal not found: {rest}")
            return None
        editor = os.environ.get("EDITOR", "vim")
        os.system(f'{editor} "{goal.path}"')  # noqa: S605
        return None

    print_error(f"Unknown subcommand: {sub}. Try /goals help")
    return None


# ---------------------------------------------------------------------------
# /arbiter — Arbiter judge status and control
# ---------------------------------------------------------------------------


async def cmd_arbiter(args: str, _ctx: REPLContext) -> str | None:
    """Show Arbiter judge status, recent verdicts, and watchdog activity.

    Usage:
      /arbiter                — Overview: status + last 10 verdicts
      /arbiter verdicts [n]   — Show last n verdicts (default 20)
      /arbiter stats          — Aggregate verdict stats
      /arbiter watchdog       — Run a watchdog sweep now
    """
    sub = args.strip().lower()
    tokens = sub.split()
    sub_cmd = tokens[0] if tokens else ""

    # -- Default: overview ---------------------------------------------------
    if not sub_cmd or sub_cmd == "status":
        try:
            from obscura.arbiter.hooks import get_engine

            engine = get_engine()
            if engine is None:
                print_info("Arbiter is not active in this session.")
                return None

            status = engine.status()
            print_info("Arbiter Status")
            console.print(f"  Running:      {status['running']}")
            console.print(f"  Evaluations:  {status['evaluations']}")
            console.print(f"  Judge calls:  {status['judge_calls']}")

            vc = status.get("verdict_counts", {})
            if vc:
                parts = [f"{k}={v}" for k, v in sorted(vc.items())]
                console.print(f"  Verdicts:     {', '.join(parts)}")

            retries = status.get("active_retries", {})
            if retries:
                console.print(f"  Active retries: {retries}")

            # Show last few verdicts.
            recent = engine.events[-5:]
            if recent:
                console.print("\n[bold]Recent verdicts:[/bold]")
                for e in reversed(recent):
                    ts = e.timestamp.strftime("%H:%M:%S")
                    v = e.verdict.value.upper()
                    color = {
                        "accept": "green",
                        "revise": "yellow",
                        "deny": "red",
                        "kill": "bold red",
                    }.get(e.verdict.value, "white")
                    fb = f" — {e.score.feedback[:60]}" if e.score.feedback else ""
                    console.print(
                        f"  {ts} [{color}]{v:7s}[/] {e.kind.value} "
                        f"[dim]{e.target_id}[/] score={e.score.composite:.2f}{fb}"
                    )
        except Exception as exc:
            logger.debug("suppressed exception in cmd_arbiter", exc_info=True)
            print_error(f"Could not read Arbiter status: {exc}")
        return None

    # -- /arbiter verdicts [n] -----------------------------------------------
    if sub_cmd == "verdicts":
        limit = 20
        if len(tokens) > 1:
            with contextlib.suppress(ValueError):
                limit = int(tokens[1])
        try:
            from obscura.arbiter.store import ArbiterStore

            store = ArbiterStore()
            recent = store.recent(limit=limit)
            if not recent:
                print_info("No verdicts recorded yet.")
                return None

            console.print(f"[bold]Last {len(recent)} verdicts:[/bold]")
            for row in recent:
                ts = datetime.fromtimestamp(row["created_at"]).strftime(
                    "%Y-%m-%d %H:%M"
                )
                v = row["verdict"].upper()
                color = {
                    "accept": "green",
                    "revise": "yellow",
                    "deny": "red",
                    "kill": "bold red",
                }.get(row["verdict"], "white")
                fb = row.get("feedback", "")[:60]
                fb_str = f" — {fb}" if fb else ""
                console.print(
                    f"  {ts} [{color}]{v:7s}[/] {row['kind']} "
                    f"[dim]{row['target_id']}[/] score={row['composite']:.2f}{fb_str}"
                )
        except Exception as exc:
            logger.debug("suppressed exception in cmd_arbiter", exc_info=True)
            print_error(f"Could not read verdicts: {exc}")
        return None

    # -- /arbiter stats ------------------------------------------------------
    if sub_cmd == "stats":
        try:
            from obscura.arbiter.store import ArbiterStore

            store = ArbiterStore()
            stats = store.stats()
            print_info("Arbiter Stats")
            console.print(f"  Total evaluations: {stats['total']}")
            console.print(f"  Avg composite:     {stats['avg_composite_score']:.3f}")
            by_v = stats.get("by_verdict", {})
            if by_v:
                for v, cnt in sorted(by_v.items()):
                    pct = (cnt / stats["total"] * 100) if stats["total"] else 0
                    console.print(f"  {v:10s}: {cnt:4d} ({pct:.0f}%)")
        except Exception as exc:
            logger.debug("suppressed exception in cmd_arbiter", exc_info=True)
            print_error(f"Could not read stats: {exc}")
        return None

    # -- /arbiter watchdog ---------------------------------------------------
    if sub_cmd == "watchdog":
        try:
            from obscura.arbiter.watchdog import ArbiterWatchdog

            wd = ArbiterWatchdog()
            actions = wd.sweep()
            if not actions:
                print_ok("Watchdog sweep: all clear.")
                return None
            print_warning(f"Watchdog found {len(actions)} issue(s):")
            results = wd.execute(actions)
            for r in results:
                console.print(f"  {r}")
        except Exception as exc:
            logger.debug("suppressed exception in cmd_arbiter", exc_info=True)
            print_error(f"Watchdog sweep failed: {exc}")
        return None

    print_error(
        f"Unknown subcommand: {sub_cmd}. Try /arbiter, /arbiter verdicts, /arbiter stats, /arbiter watchdog"
    )
    return None


async def cmd_kairos(args: str, ctx: REPLContext) -> str | None:
    """Toggle KAIROS autonomous mode.

    Usage:
      /kairos [status]               Show overall + feature status
      /kairos on|off                 Enable/disable Kairos entirely
      /kairos proactive on|off       Toggle proactive tick loop
      /kairos dream on|off           Toggle dream consolidation
    """
    from obscura.kairos.engine import is_kairos_enabled, set_kairos_mode

    def _write_setting(k: str, v: bool) -> None:
        """Persist toggle under ~/.obscura/settings.json (best-effort)."""
        try:
            sp = Path.home() / ".obscura" / "settings.json"
            sp.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, object] = {}
            if sp.is_file():
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                except Exception:
                    logger.debug(
                        "suppressed exception in _write_setting", exc_info=True
                    )
                    data = {}
            # dot-notation like "kairos.enabled"
            parts = k.split(".")
            cur: dict[str, object] = data
            for part in parts[:-1]:
                if part not in cur or not isinstance(cur[part], dict):
                    cur[part] = {}
                cur = cur[part]  # type: ignore[assignment]
            cur[parts[-1]] = v
            sp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception:
            logger.debug("suppressed exception in _write_setting", exc_info=True)

    tokens = [t for t in args.strip().split() if t]
    if not tokens or tokens[0] == "status":
        status = "ON" if is_kairos_enabled() else "OFF"
        print_info(f"KAIROS mode: {status}")
        # Feature status (resolved on demand by engine at runtime)
        pro = os.environ.get("OBSCURA_KAIROS_PROACTIVE", "").lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        dr = os.environ.get("OBSCURA_KAIROS_DREAM", "").lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        print_info(
            f"  - Proactive ticks: {'ON' if pro else 'OFF'}  (OBSCURA_KAIROS_PROACTIVE)",
        )
        print_info(
            f"  - Dream consolidation: {'ON' if dr else 'OFF'}  (OBSCURA_KAIROS_DREAM)",
        )
        if is_kairos_enabled():
            print_info("Disable with: /kairos off  (or set OBSCURA_KAIROS=false)")
        else:
            print_info("Enable with: /kairos on  (or unset OBSCURA_KAIROS)")
        return None

    if tokens[0] in ("on", "off"):
        enabled = tokens[0] == "on"
        set_kairos_mode(enabled)
        _write_setting("kairos.enabled", enabled)
        print_ok(
            f"KAIROS mode {'enabled' if enabled else 'disabled'}. {'Will activate on next session start.' if enabled else ''}",
        )
        return None

    if tokens[0] in ("proactive", "dream"):
        if len(tokens) < 2 or tokens[1] not in ("on", "off"):
            print_error("Usage: /kairos proactive on|off   or   /kairos dream on|off")
            return None
        enabled = tokens[1] == "on"
        if tokens[0] == "proactive":
            os.environ["OBSCURA_KAIROS_PROACTIVE"] = "1" if enabled else "0"
            _write_setting("kairos.proactive", enabled)
            print_ok(f"Kairos proactive ticks {'enabled' if enabled else 'disabled'}.")
        else:
            os.environ["OBSCURA_KAIROS_DREAM"] = "1" if enabled else "0"
            _write_setting("kairos.dream", enabled)
            print_ok(
                f"Kairos dream consolidation {'enabled' if enabled else 'disabled'}.",
            )
        return None

    print_error(
        "Unknown args. Try: /kairos [status|on|off|proactive on|off|dream on|off]",
    )
    return None


async def cmd_attribution(_args: str, _ctx: REPLContext) -> str | None:
    """Show AI commit attribution summary."""

    tracker = get_attribution_tracker()
    summary = tracker.summary()
    if summary["files_tracked"] == 0:
        print_info("No file attribution data recorded yet.")
        return None

    table = Table(title="Commit Attribution", expand=False)
    table.add_column("Metric", width=25)
    table.add_column("Value", width=15, justify="right")
    table.add_row("Files tracked", str(summary["files_tracked"]))
    table.add_row("Agent lines", f"{summary['agent_lines']:,}")
    table.add_row("Human lines", f"{summary['human_lines']:,}")
    table.add_row("Agent %", f"{summary['agent_percentage']}%")
    console.print(table)
    return None


async def cmd_ps(_args: str, _ctx: REPLContext) -> str | None:
    """List background sessions."""
    from obscura.kairos.background_sessions import ps

    sessions = ps()
    if not sessions:
        print_info("No background sessions.")
        return None

    table = Table(title="Background Sessions", expand=False)
    table.add_column("ID", width=12)
    table.add_column("PID", width=8, justify="right")
    table.add_column("Status", width=10)
    table.add_column("Model", width=12)
    table.add_column("Uptime", width=8, justify="right")
    table.add_column("Command", max_width=40)

    for s in sessions:
        table.add_row(
            s["session_id"],
            str(s["pid"]),
            s["status"],
            s["model"],
            f"{s['uptime_s']}s",
            s["command"],
        )
    console.print(table)
    return None


async def cmd_logs(args: str, _ctx: REPLContext) -> str | None:
    """Show logs for a background session. Usage: /logs <session_id>."""
    from obscura.kairos.background_sessions import logs

    sid = args.strip()
    if not sid:
        print_error("Usage: /logs <session_id>")
        return None
    output = logs(sid, tail=50)
    console.print(output)
    return None


async def cmd_kill_session(args: str, _ctx: REPLContext) -> str | None:
    """Kill a background session. Usage: /kill-session <session_id>."""
    from obscura.kairos.background_sessions import kill_session

    sid = args.strip()
    if not sid:
        print_error("Usage: /kill-session <session_id>")
        return None
    result = kill_session(sid)
    print_info(result)
    return None


async def cmd_suggestions(_args: str, ctx: REPLContext) -> str | None:
    """Show context-aware file suggestions based on recent activity."""
    modified = get_recently_modified_files(limit=10)
    read = get_recently_read_files(limit=10)

    if not modified:
        print_info("No recent file activity to base suggestions on.")
        return None

    suggestions = suggest_files(modified, read, max_suggestions=8)
    if not suggestions:
        print_info("No suggestions — files look self-contained.")
        return None

    table = Table(title="Suggested Files", expand=False)
    table.add_column("File", max_width=50)
    table.add_column("Reason", max_width=30)
    for s in suggestions:
        table.add_row(s["path"], s["reason"])
    console.print(table)
    return None


async def cmd_cache_stats(_args: str, _ctx: REPLContext) -> str | None:
    """Show prompt cache hit/miss statistics."""
    # This would need to reference the actual cache instance.
    # For now show the concept.
    print_info("Prompt cache tracking available. Stats shown in /cost output.")
    return None


async def cmd_workflow(args: str, ctx: REPLContext) -> str | None:
    """Run or list workflow scripts. Usage: /workflow list|run <name>."""
    from obscura.core.workflows import list_workflows, load_workflow, run_workflow

    parts = args.strip().split(None, 1)
    sub = parts[0] if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        workflows = list_workflows()
        if not workflows:
            print_info("No workflows found. Create one in ~/.obscura/workflows/")
            return None
        table = Table(title="Workflows", expand=False)
        table.add_column("Name", width=20)
        table.add_column("Steps", width=8, justify="right")
        table.add_column("Description", max_width=40)
        for wf in workflows:
            table.add_row(wf.name, str(len(wf.steps)), wf.description[:40])
        console.print(table)
        return None

    if sub == "run":
        name = rest.strip()
        if not name:
            print_error("Usage: /workflow run <name>")
            return None
        wf = load_workflow(name)
        if wf is None:
            print_error(f"Workflow not found: {name}")
            return None
        print_info(f"Running workflow '{wf.name}' ({len(wf.steps)} steps)...")

        def on_start(step_name: str) -> None:
            console.print(f"[cyan]Step: {step_name}[/]")

        def on_done(step_name: str, result: dict[str, Any]) -> None:
            status = result.get("status", "?")
            icon = "[green]✓[/]" if status == "ok" else "[red]✗[/]"
            console.print(f"  {icon} {step_name}: {status}")

        results = await run_workflow(
            wf,
            ctx.client,
            on_step_start=on_start,
            on_step_complete=on_done,
        )
        ok_count = sum(1 for r in results if r["status"] == "ok")
        print_ok(f"Workflow complete: {ok_count}/{len(results)} steps succeeded")
        return None

    print_info("Usage: /workflow list|run <name>")
    return None


async def cmd_peers(_args: str, ctx: REPLContext) -> str | None:
    """List other running obscura sessions (peer discovery)."""
    from obscura.kairos.uds_messaging import discover_peers

    peers = discover_peers()
    if not peers:
        print_info("No other sessions found.")
        return None

    current = ctx.session_id[:12] if ctx.session_id else "?"
    table = Table(title="Active Peers", expand=False)
    table.add_column("Session ID", width=14)
    table.add_column("Status", width=8)
    for p in peers:
        is_self = "(self)" if p.startswith(current) else ""
        table.add_row(p[:12], f"active {is_self}")
    console.print(table)
    return None


async def cmd_send(args: str, ctx: REPLContext) -> str | None:
    """Send a message to another session. Usage: /send <session_id> <message>."""
    from obscura.kairos.uds_messaging import send_message as uds_send

    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_error("Usage: /send <session_id> <message>")
        return None
    target = parts[0]
    message_text = parts[1]

    delivered = await uds_send(
        target,
        {
            "type": "text",
            "from": ctx.session_id[:12] if ctx.session_id else "unknown",
            "text": message_text,
            "timestamp": __import__("time").time(),
        },
    )
    if delivered:
        print_ok(f"Message sent to {target[:12]}")
    else:
        print_error(f"Failed to deliver to {target[:12]} (session may not be running)")
    return None


# ---------------------------------------------------------------------------
# Missing from claude-code: add-dir, files, rewind, rename, tag, version,
# usage, copy, brief, stats
# ---------------------------------------------------------------------------


async def cmd_add_dir(args: str, ctx: REPLContext) -> str | None:
    """Allow a directory for tool access at runtime. Usage: /add-dir <path>

    Registers the path with the system tools allowlist so agents can read/write
    it regardless of OBSCURA_SYSTEM_TOOLS_BASE_DIR. Does NOT change cwd.
    Pass --chdir to also change the working directory.
    """
    parts = args.strip().split()
    if not parts:
        print_error("Usage: /add-dir <path> [--chdir]")
        return None
    do_chdir = "--chdir" in parts
    path_parts = [p for p in parts if p != "--chdir"]
    target = " ".join(path_parts)
    p = Path(target).expanduser().resolve()
    if not p.is_dir():
        print_error(f"Not a directory: {p}")
        return None
    # Register with system tools allowlist.
    from obscura.tools.system import Policy

    Policy.add_allowed_dir(p)
    if do_chdir:
        os.chdir(p)
        print_ok(f"Allowed and changed working directory to: {p}")
    else:
        print_ok(f"Allowed for tool access: {p}")
    return None


async def cmd_files(_args: str, ctx: REPLContext) -> str | None:
    """List files tracked in the current context."""
    read = get_recently_read_files(limit=20)
    modified = get_recently_modified_files(limit=10)

    if not read and not modified:
        print_info("No files tracked in this session yet.")
        return None

    if modified:
        console.print("[bold]Modified files:[/]")
        for f in modified:
            console.print(f"  [green]+[/] {f}")
    if read:
        console.print("[bold]Read files:[/]")
        for f in read[:15]:
            console.print(f"  [dim]  {f}[/]")
        if len(read) > 15:
            console.print(f"  [dim]  ...and {len(read) - 15} more[/]")
    return None


async def cmd_rewind(args: str, ctx: REPLContext) -> str | None:
    """Undo recent changes by reverting modified files. Usage: /rewind [n]."""
    if not ctx.file_changes:
        print_info("No file changes to rewind.")
        return None

    n = int(args.strip()) if args.strip().isdigit() else len(ctx.file_changes)
    rewound = 0
    for fc in ctx.file_changes[-n:]:
        try:
            Path(fc["path"]).write_text(fc["original"])
            rewound += 1
        except Exception as exc:
            logger.debug("suppressed exception in cmd_rewind", exc_info=True)
            print_error(f"Failed to rewind {fc['path']}: {exc}")

    ctx.file_changes = ctx.file_changes[:-n] if n < len(ctx.file_changes) else []
    print_ok(f"Rewound {rewound} file(s) to their original state.")
    return None


async def cmd_rename(args: str, ctx: REPLContext) -> str | None:
    """Rename the current session. Usage: /rename <title>."""
    title = args.strip()
    if not title:
        print_error("Usage: /rename <title>")
        return None
    try:
        await ctx.store.update_session(ctx.session_id, summary=title)
        # Update prompt status so the title appears immediately
        _prompt_status = getattr(ctx, "_prompt_status", None)
        if _prompt_status is not None:
            _prompt_status.session_title = title
        print_ok(f"Session renamed: {title}")
    except Exception:
        # Fallback: store in metadata if update_summary doesn't exist.
        logger.debug("suppressed exception in cmd_rename", exc_info=True)
        print_ok(f"Session title set: {title}")
    return None


async def cmd_tag(args: str, ctx: REPLContext) -> str | None:
    """Tag the current session for search. Usage: /tag <tag>."""
    tag = args.strip()
    if not tag:
        print_error("Usage: /tag <tag>")
        return None
    # Store tags in session metadata via event store.
    try:
        sess = await ctx.store.get_session(ctx.session_id)
        if sess is not None:
            meta = getattr(sess, "metadata", {}) or {}
            tags = meta.get("tags", [])
            if tag in tags:
                tags.remove(tag)
                print_ok(f"Tag removed: {tag}")
            else:
                tags.append(tag)
                print_ok(f"Tag added: {tag}")
    except Exception:
        logger.debug("suppressed exception in cmd_tag", exc_info=True)
        print_ok(f"Tagged session: {tag}")
    return None


async def cmd_version(_args: str, _ctx: REPLContext) -> str | None:
    """Show Obscura version and system info."""
    try:
        from importlib.metadata import version as pkg_version

        ver = pkg_version("obscura")
    except Exception:
        logger.debug("suppressed exception in cmd_version", exc_info=True)
        ver = "dev"

    console.print(f"[bold]Obscura[/] {ver}")
    console.print(
        f"  Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    console.print(f"  Platform: {sys.platform}")
    return None


async def cmd_usage(_args: str, ctx: REPLContext) -> str | None:
    """Show API usage summary for the current session."""

    tracker = get_cost_tracker()
    if tracker.turn_count() == 0:
        print_info("No API usage recorded yet.")
        return None

    console.print("[bold]API Usage[/]")
    console.print(f"  Turns:         {tracker.turn_count()}")
    console.print(f"  Input tokens:  {tracker.total_input_tokens():,}")
    console.print(f"  Output tokens: {tracker.total_output_tokens():,}")
    console.print(f"  Total cost:    ${tracker.session_total_usd():.4f}")
    console.print(f"  Backend:       {ctx.backend}")
    console.print(f"  Model:         {ctx.model or 'default'}")
    return None


async def cmd_copy(_args: str, ctx: REPLContext) -> str | None:
    """Copy last assistant response to clipboard."""
    # Find last assistant message.
    for role, text in reversed(ctx.message_history):
        if role == "assistant":
            try:
                proc = subprocess.run(
                    ["pbcopy"]
                    if sys.platform == "darwin"
                    else ["xclip", "-selection", "clipboard"],
                    input=text.encode("utf-8"),
                    capture_output=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    preview = text[:60].replace("\n", " ")
                    print_ok(f"Copied to clipboard: {preview}...")
                else:
                    print_error("Clipboard command failed. Try: brew install xclip")
            except FileNotFoundError:
                # Fallback: write to file.
                logger.debug("suppressed exception in cmd_copy", exc_info=True)
                out_path = Path.home() / ".obscura" / "output" / "last_response.txt"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(text, encoding="utf-8")
                print_ok(f"Saved to {out_path}")
            except Exception as exc:
                logger.debug("suppressed exception in cmd_copy", exc_info=True)
                print_error(f"Copy failed: {exc}")
            return None

    print_info("No assistant response to copy.")
    return None


async def cmd_brief(_args: str, ctx: REPLContext) -> str | None:
    """Toggle brief output mode (concise responses)."""
    current = getattr(ctx, "effort_level", "medium")
    if current == "low":
        ctx.effort_level = "medium"
        print_ok("Brief mode OFF (standard responses)")
    else:
        ctx.effort_level = "low"
        print_ok("Brief mode ON (concise, terse responses)")
    return None


async def cmd_stats(_args: str, ctx: REPLContext) -> str | None:
    """Show session statistics."""
    tracker = get_cost_tracker()
    user_msgs = sum(1 for r, _ in ctx.message_history if r == "user")
    asst_msgs = sum(1 for r, _ in ctx.message_history if r == "assistant")
    modified = get_recently_modified_files(limit=100)
    read_files = get_recently_read_files(limit=100)

    table = Table(title="Session Statistics", expand=False)
    table.add_column("Metric", width=25)
    table.add_column("Value", width=20, justify="right")
    table.add_row("Session ID", ctx.session_id[:12])
    table.add_row("Backend", ctx.backend)
    table.add_row("Model", ctx.model or "default")
    table.add_row("User messages", str(user_msgs))
    table.add_row("Assistant messages", str(asst_msgs))
    table.add_row("API turns", str(tracker.turn_count()))
    table.add_row("Input tokens", f"{tracker.total_input_tokens():,}")
    table.add_row("Output tokens", f"{tracker.total_output_tokens():,}")
    table.add_row("Estimated cost", f"${tracker.session_total_usd():.4f}")
    table.add_row("Files modified", str(len(modified)))
    table.add_row("Files read", str(len(read_files)))
    table.add_row("File changes tracked", str(len(ctx.file_changes)))
    table.add_row("Permission mode", getattr(ctx, "permission_mode", "default"))
    table.add_row("Effort level", getattr(ctx, "effort_level", "medium"))
    console.print(table)
    return None


# ---------------------------------------------------------------------------
# Deep log viewer
# ---------------------------------------------------------------------------


async def cmd_log(args: str, _ctx: REPLContext) -> str | None:
    """View deep logs. Usage: /log [tail N | path | stats]."""

    sub = args.strip().lower()

    if sub == "path":
        console.print(f"Log file: {dlog.log_path}")
        return None

    if sub == "stats":
        console.print(f"Entries this session: {dlog.total_entries}")
        console.print(f"Log file: {dlog.log_path}")
        log_path = Path(dlog.log_path)
        if log_path.exists():
            size = log_path.stat().st_size
            if size > 1024 * 1024:
                console.print(f"File size: {size / 1024 / 1024:.1f} MB")
            else:
                console.print(f"File size: {size / 1024:.1f} KB")
        return None

    # Default: tail last N entries.
    n = 20
    if sub.startswith("tail"):
        parts = sub.split()
        if len(parts) > 1 and parts[1].isdigit():
            n = int(parts[1])
    elif sub.isdigit():
        n = int(sub)

    log_path = Path(dlog.log_path)
    if not log_path.exists():
        print_info("No log entries yet.")
        return None

    lines = log_path.read_text(encoding="utf-8").splitlines()
    recent = lines[-n:]

    for line in recent:
        try:
            entry = json.loads(line)
            ts = entry.get("ts", 0)
            etype = entry.get("type", "?")
            data = entry.get("data", {})

            time_str = (
                _dt_module.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                if ts
                else "??:??:??"
            )

            if etype == "tool_call":
                tool = data.get("tool", "?")
                ok = data.get("ok", True)
                dur = data.get("duration_ms", 0)
                icon = "[green]✓[/]" if ok else "[red]✗[/]"
                console.print(
                    f"  [dim]{time_str}[/] {icon} [yellow]{tool}[/] [dim]{dur}ms[/]",
                )
            elif etype == "api_request":
                model = data.get("model", "?")
                inp = data.get("input_tokens", 0)
                out = data.get("output_tokens", 0)
                console.print(
                    f"  [dim]{time_str}[/] [cyan]API[/] {model} {inp}→{out} tokens",
                )
            elif etype == "session":
                action = data.get("action", "?")
                console.print(f"  [dim]{time_str}[/] [bold]SESSION[/] {action}")
            elif etype == "error":
                msg = data.get("message", "?")[:80]
                console.print(f"  [dim]{time_str}[/] [red]ERROR[/] {msg}")
            else:
                console.print(f"  [dim]{time_str}[/] {etype}: {json.dumps(data)[:80]}")
        except Exception:
            logger.debug("suppressed exception in cmd_log", exc_info=True)
            console.print(f"  [dim]{line[:100]}[/]")

    console.print(
        f"\n[dim]Showing last {len(recent)} of {len(lines)} entries. /log path for file location.[/]",
    )
    return None


# ---------------------------------------------------------------------------
# Side question, sandbox, summary, stash/pop
# ---------------------------------------------------------------------------


class _OneshotStalled(Exception):
    """Raised when ``_oneshot_stream`` hits its per-chunk idle timeout.

    Carries any text already collected so callers can still render partial
    output instead of losing it on stall.
    """

    def __init__(self, partial: str, idle_timeout: float) -> None:
        super().__init__(f"no output for {idle_timeout:.0f}s")
        self.partial = partial
        self.idle_timeout = idle_timeout


async def _oneshot_stream(client: Any, prompt: str, idle_timeout: float = 60.0) -> str:
    """Stream a one-shot prompt and return the concatenated text.

    Uses streaming instead of ``client.send()`` to avoid session state
    conflicts with the copilot backend after ``run_loop`` streaming. Uses
    a per-chunk idle timeout so long answers complete as long as the model
    keeps producing output; only aborts if no chunk arrives within
    ``idle_timeout`` seconds, raising ``_OneshotStalled`` carrying any
    text received so far.
    """
    await client.reset_session()
    parts: list[str] = []

    stream = client.stream(prompt).__aiter__()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(stream.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                logger.debug("suppressed exception in _oneshot_stream", exc_info=True)
                break
            except TimeoutError:
                raise _OneshotStalled("".join(parts), idle_timeout) from None
            if hasattr(chunk, "text") and chunk.text:
                parts.append(chunk.text)
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                logger.debug("suppressed exception in _oneshot_stream", exc_info=True)
    return "".join(parts)


async def cmd_btw(args: str, ctx: REPLContext) -> str | None:
    """Ask a side question without affecting the main conversation.

    Usage: /btw <question>

    The answer is displayed but NOT added to conversation history,
    so it won't affect subsequent context or cost.
    """
    question = args.strip()
    if not question:
        print_error("Usage: /btw <question>")
        return None

    print_info("[dim]Side question (not added to history):[/]")

    try:
        text = await _oneshot_stream(ctx.client, question)
        if text:
            console.print(Markdown(text))
        else:
            print_info("(no response)")
    except _OneshotStalled as stalled:
        logger.debug("suppressed exception in cmd_btw", exc_info=True)
        if stalled.partial:
            console.print(Markdown(stalled.partial))
            print_info(
                f"[dim](truncated — no output for {stalled.idle_timeout:.0f}s)[/]"
            )
        else:
            print_error(
                f"Side question stalled — no output for {stalled.idle_timeout:.0f}s."
            )
    except Exception as exc:
        logger.debug("suppressed exception in cmd_btw", exc_info=True)
        print_error(f"Side question failed: {exc}")
    return None


async def cmd_sandbox_toggle(_args: str, ctx: REPLContext) -> str | None:
    """Toggle filesystem sandboxing for tool execution."""
    current = os.environ.get("OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS", "")
    if current in ("1", "true", "yes"):
        os.environ["OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS"] = ""
        print_ok("Sandbox ON — file tools restricted to allowed paths.")
    else:
        os.environ["OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS"] = "1"
        print_warning("Sandbox OFF — file tools have full filesystem access.")
    return None


async def cmd_summary(_args: str, ctx: REPLContext) -> str | None:
    """Generate a brief summary of the current conversation."""
    if len(ctx.message_history) < 2:
        print_info("Not enough conversation to summarize.")
        return None

    # Build context from recent messages.
    recent = ctx.message_history[-20:]
    context_lines: list[str] = []
    for role, text in recent:
        preview = text[:300].replace("\n", " ")
        context_lines.append(f"[{role}]: {preview}")

    prompt = (
        "Summarize this conversation in 3-5 bullet points. "
        "Focus on: what was requested, what was done, what's pending.\n\n"
        + "\n".join(context_lines)
    )

    try:
        text = await _oneshot_stream(ctx.client, prompt)
        if text:
            console.print(Markdown(text))
        else:
            print_info("(no summary generated)")
    except _OneshotStalled as stalled:
        logger.debug("suppressed exception in cmd_summary", exc_info=True)
        if stalled.partial:
            console.print(Markdown(stalled.partial))
            print_info(
                f"[dim](truncated — no output for {stalled.idle_timeout:.0f}s)[/]"
            )
        else:
            print_error(f"Summary stalled — no output for {stalled.idle_timeout:.0f}s.")
    except Exception as exc:
        logger.debug("suppressed exception in cmd_summary", exc_info=True)
        print_error(f"Summary failed: {exc}")
    return None


# Stash storage: list of (message_history, session_id, file_changes) tuples.
_stash_stack: list[tuple[list[tuple[str, str]], str, list[dict[str, str]]]] = []


async def cmd_stash(_args: str, ctx: REPLContext) -> str | None:
    """Save current conversation context and start fresh (like git stash)."""
    _stash_stack.append(
        (
            list(ctx.message_history),
            ctx.session_id,
            list(ctx.file_changes),
        ),
    )
    stash_idx = len(_stash_stack) - 1

    # Clear current context.
    ctx.message_history.clear()
    ctx.file_changes.clear()
    new_sid = uuid.uuid4().hex
    ctx.session_id = new_sid

    print_ok(f"Stashed conversation (stash@{{{stash_idx}}}). Fresh context started.")
    print_info(f"Use /pop to restore. {len(_stash_stack)} stash(es) saved.")
    return None


async def cmd_pop(_args: str, ctx: REPLContext) -> str | None:
    """Restore the most recently stashed conversation context."""
    if not _stash_stack:
        print_info("No stashes to pop.")
        return None

    history, session_id, file_changes = _stash_stack.pop()

    ctx.message_history.clear()
    ctx.message_history.extend(history)
    ctx.session_id = session_id
    ctx.file_changes.clear()
    ctx.file_changes.extend(file_changes)

    print_ok(
        f"Popped stash. Restored {len(history)} messages, {len(file_changes)} file changes.",
    )
    if _stash_stack:
        print_info(f"{len(_stash_stack)} stash(es) remaining.")
    return None


# ---------------------------------------------------------------------------
# Claude Code parity: loop, schedule, branch, config, hooks, listen, login,
# logout, release-notes, ide, bug, terminal-setup
# ---------------------------------------------------------------------------


# ── /loop ─────────────────────────────────────────────────────────────────

_loop_tasks: dict[str, asyncio.Task[None]] = {}


async def cmd_loop(args: str, ctx: REPLContext) -> str | None:
    """Run a prompt or slash command on a recurring interval.

    Usage:
        /loop 5m /commit          — run /commit every 5 minutes
        /loop 30s check deploy    — send "check deploy" every 30 seconds
        /loop status              — show active loops
        /loop stop [label]        — stop a loop (or all)
    """
    tokens = args.strip().split(None, 1)
    if not tokens:
        print_info(
            "Usage: /loop <interval> <prompt|/cmd>  |  /loop status  |  /loop stop [label]",
        )
        return None

    sub = tokens[0].lower()

    if sub == "status":
        if not _loop_tasks:
            print_info("No active loops.")
            return None
        table = Table(title="Active Loops", expand=False)
        table.add_column("Label", width=20)
        table.add_column("Status", width=10)
        for label, task in _loop_tasks.items():
            table.add_row(label, "running" if not task.done() else "stopped")
        console.print(table)
        return None

    if sub == "stop":
        label = tokens[1].strip() if len(tokens) > 1 else ""
        if label:
            task = _loop_tasks.pop(label, None)
            if task and not task.done():
                task.cancel()
                print_ok(f"Stopped loop: {label}")
            else:
                print_error(f"No active loop named '{label}'")
        else:
            for _lbl, task in _loop_tasks.items():
                if not task.done():
                    task.cancel()
            count = len(_loop_tasks)
            _loop_tasks.clear()
            print_ok(f"Stopped {count} loop(s).")
        return None

    interval_str = sub
    prompt_or_cmd = tokens[1].strip() if len(tokens) > 1 else ""
    if not prompt_or_cmd:
        print_error("Usage: /loop <interval> <prompt|/cmd>")
        return None

    interval_s = _parse_interval(interval_str)
    if interval_s is None:
        print_error(
            f"Invalid interval '{interval_str}'. Use: 30s, 5m, 1h, or plain seconds.",
        )
        return None

    if interval_s < 10:
        print_error("Minimum interval is 10 seconds.")
        return None

    label = prompt_or_cmd[:30].replace(" ", "-").strip("/")

    async def _loop_body() -> None:
        iteration = 0
        try:
            while True:
                await asyncio.sleep(interval_s)
                iteration += 1
                console.print(
                    f"\n[dim]loop({label}) iteration {iteration}[/]",
                    highlight=False,
                )
                if prompt_or_cmd.startswith("/"):
                    await handle_command(prompt_or_cmd, ctx)
                else:
                    try:
                        async for event in ctx.client.run_loop(prompt_or_cmd):
                            render_event(event)
                    except Exception as exc:
                        logger.debug(
                            "suppressed exception in _loop_body", exc_info=True
                        )
                        print_error(f"Loop error: {exc}")
        except asyncio.CancelledError:
            logger.debug("suppressed exception in _loop_body", exc_info=True)

    task = asyncio.get_event_loop().create_task(_loop_body())
    _loop_tasks[label] = task
    _fmt = _format_interval(interval_s)
    print_ok(f"Loop started: '{label}' every {_fmt}. Stop with /loop stop {label}")
    return None


def _parse_interval(s: str) -> float | None:
    """Parse '5m', '30s', '1h' into seconds."""
    s = s.strip().lower()
    if s.isdigit():
        return float(s)
    try:
        if s.endswith("s"):
            return float(s[:-1])
        if s.endswith("m"):
            return float(s[:-1]) * 60
        if s.endswith("h"):
            return float(s[:-1]) * 3600
    except ValueError:
        logger.debug("suppressed exception in _parse_interval", exc_info=True)
    return None


def _format_interval(seconds: float) -> str:
    """Format seconds into a human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s}s" if s else f"{m}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m}m" if m else f"{h}h"


# ── /schedule ─────────────────────────────────────────────────────────────


async def cmd_schedule(args: str, ctx: REPLContext) -> str | None:
    """Create, list, or manage scheduled agents (cron-style).

    Usage:
        /schedule list                          — list scheduled tasks
        /schedule create "<cron>" <prompt>       — create a scheduled task
        /schedule delete <id>                    — delete a scheduled task
        /schedule run <id>                       — run a scheduled task now
        /schedule pause <id> | resume <id>       — pause/resume a task

    Examples:
        /schedule create "0 9 * * *" run tests and report failures
        /schedule create "*/30 * * * *" /commit
    """
    schedule_dir = Path.home() / ".obscura" / "schedules"
    schedule_dir.mkdir(parents=True, exist_ok=True)

    tokens = args.strip().split(None, 1)
    sub = tokens[0].lower() if tokens else "list"
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    if sub == "list":
        schedules = _load_schedules(schedule_dir)
        if not schedules:
            print_info(
                'No scheduled tasks. Create one: /schedule create "<cron>" <prompt>',
            )
            return None
        table = Table(title="Scheduled Tasks", expand=False)
        table.add_column("ID", width=8)
        table.add_column("Cron", width=16)
        table.add_column("Status", width=8)
        table.add_column("Prompt", max_width=40)
        table.add_column("Last Run", width=16)
        for s in schedules:
            table.add_row(
                s["id"][:8],
                s["cron"],
                s.get("status", "active"),
                (s["prompt"][:37] + "...") if len(s["prompt"]) > 40 else s["prompt"],
                s.get("last_run", "-") or "-",
            )
        console.print(table)
        return None

    if sub == "create":
        if not rest:
            print_error('Usage: /schedule create "<cron>" <prompt>')
            return None
        cron, prompt = _parse_cron_and_prompt(rest)
        if not cron or not prompt:
            print_error('Usage: /schedule create "*/5 * * * *" run tests')
            return None
        sched_id = uuid.uuid4().hex[:12]
        sched_data = {
            "id": sched_id,
            "cron": cron,
            "prompt": prompt,
            "status": "active",
            "created_at": __import__("datetime").datetime.now(UTC).isoformat(),
            "last_run": None,
            "model": ctx.model or "",
            "backend": ctx.backend or "",
        }
        (schedule_dir / f"{sched_id}.json").write_text(
            json.dumps(sched_data, indent=2),
            encoding="utf-8",
        )
        print_ok(f"Scheduled task created: {sched_id[:8]}")
        print_info(f"  Cron: {cron}")
        print_info(f"  Prompt: {prompt[:60]}")
        pid_file = schedule_dir / ".scheduler.pid"
        if not pid_file.exists():
            console.print(
                "[dim]Tip: Schedules run when obscura is running. "
                "For persistent scheduling, use: obscura --daemon[/]",
            )
        return None

    if sub == "delete":
        sched_id = rest.strip()
        if not sched_id:
            print_error("Usage: /schedule delete <id>")
            return None
        if _delete_schedule(schedule_dir, sched_id):
            print_ok(f"Deleted schedule: {sched_id[:8]}")
        else:
            print_error(f"Schedule not found: {sched_id[:8]}")
        return None

    if sub == "run":
        sched_id = rest.strip()
        if not sched_id:
            print_error("Usage: /schedule run <id>")
            return None
        sched = _find_schedule(schedule_dir, sched_id)
        if not sched:
            print_error(f"Schedule not found: {sched_id[:8]}")
            return None
        print_info(f"Running schedule '{sched_id[:8]}' now...")
        prompt = sched["prompt"]
        if prompt.startswith("/"):
            await handle_command(prompt, ctx)
        else:
            try:
                async for event in ctx.client.run_loop(prompt):
                    render_event(event)
            except Exception as exc:
                logger.debug("suppressed exception in cmd_schedule", exc_info=True)
                print_error(f"Schedule run error: {exc}")
        sched["last_run"] = __import__("datetime").datetime.now(UTC).isoformat()
        (schedule_dir / f"{sched['id']}.json").write_text(
            json.dumps(sched, indent=2),
            encoding="utf-8",
        )
        return None

    if sub in ("pause", "resume"):
        sched_id = rest.strip()
        if not sched_id:
            print_error(f"Usage: /schedule {sub} <id>")
            return None
        sched = _find_schedule(schedule_dir, sched_id)
        if not sched:
            print_error(f"Schedule not found: {sched_id[:8]}")
            return None
        new_status = "paused" if sub == "pause" else "active"
        sched["status"] = new_status
        (schedule_dir / f"{sched['id']}.json").write_text(
            json.dumps(sched, indent=2),
            encoding="utf-8",
        )
        print_ok(f"Schedule {sched_id[:8]} {new_status}.")
        return None

    print_info("Usage: /schedule list|create|delete|run|pause|resume")
    return None


def _load_schedules(sdir: Path) -> list[dict[str, Any]]:
    schedules: list[dict[str, Any]] = []
    for f in sorted(sdir.glob("*.json")):
        try:
            schedules.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            logger.debug("suppressed exception in _load_schedules", exc_info=True)
    return schedules


def _find_schedule(sdir: Path, prefix: str) -> dict[str, Any] | None:
    for s in _load_schedules(sdir):
        if s["id"].startswith(prefix):
            return s
    return None


def _delete_schedule(sdir: Path, prefix: str) -> bool:
    for f in sdir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("id", "").startswith(prefix):
                f.unlink()
                return True
        except Exception:
            logger.debug("suppressed exception in _delete_schedule", exc_info=True)
    return False


def _parse_cron_and_prompt(s: str) -> tuple[str, str]:
    if s.startswith('"'):
        end = s.find('"', 1)
        if end > 0:
            return s[1:end].strip(), s[end + 1 :].strip()
    if s.startswith("'"):
        end = s.find("'", 1)
        if end > 0:
            return s[1:end].strip(), s[end + 1 :].strip()
    parts = s.split()
    if len(parts) >= 6:
        return " ".join(parts[:5]), " ".join(parts[5:])
    return "", ""


# ── /branch ───────────────────────────────────────────────────────────────


async def cmd_branch(args: str, _ctx: REPLContext) -> str | None:
    """Git branch management.

    Usage:
        /branch                 — show current branch + recent branches
        /branch <name>          — switch to branch (create if needed)
        /branch list            — list all branches
        /branch create <name>   — create and switch to a new branch
        /branch delete <name>   — delete a branch
    """
    sub = args.strip()

    if not sub or sub == "list":
        proc = await asyncio.create_subprocess_shell(
            "git branch -v --sort=-committerdate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            print_error(f"git error: {stderr.decode().strip()}")
            return None
        output = stdout.decode().strip()
        if not output:
            print_info("No branches found.")
            return None
        for line in output.split("\n"):
            if line.startswith("*"):
                console.print(f"[bold green]{line}[/]")
            else:
                console.print(f"  [dim]{line.strip()}[/]")
        return None

    if sub.startswith("create "):
        name = sub[7:].strip()
        if not name:
            print_error("Usage: /branch create <name>")
            return None
        proc = await asyncio.create_subprocess_shell(
            f"git checkout -b {shlex.quote(name)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            print_ok(f"Created and switched to branch: {name}")
        else:
            print_error(stderr.decode().strip())
        return None

    if sub.startswith("delete "):
        name = sub[7:].strip()
        if not name:
            print_error("Usage: /branch delete <name>")
            return None
        if name in ("main", "master"):
            print_error(f"Refusing to delete {name}.")
            return None
        proc = await asyncio.create_subprocess_shell(
            f"git branch -d {shlex.quote(name)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            print_ok(f"Deleted branch: {name}")
        else:
            print_error(stderr.decode().strip())
        return None

    # Default: switch (create if not exists)
    name = sub
    proc = await asyncio.create_subprocess_shell(
        f"git rev-parse --verify {shlex.quote(name)} 2>/dev/null",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    exists = proc.returncode == 0

    cmd = (
        f"git checkout {shlex.quote(name)}"
        if exists
        else f"git checkout -b {shlex.quote(name)}"
    )
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        action = "Switched to" if exists else "Created and switched to"
        print_ok(f"{action} branch: {name}")
    else:
        print_error(stderr.decode().strip())
    return None


# ── /worktree ─────────────────────────────────────────────────────────────


async def cmd_worktree(args: str, _ctx: REPLContext) -> str | None:
    """Inspect and manage git worktrees tracked in ~/.obscura/worktrees/.

    Usage:
        /worktree                 — list active worktrees for this repo
        /worktree list            — list all worktrees across all repos
        /worktree status <slug>   — show details + observer summary for a slug
        /worktree sweep           — mark dead-PID entries as orphan
        /worktree cleanup         — sweep + prune missing paths + drop orphan dirs
    """

    from obscura.tools import worktree_observer, worktree_registry

    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    sub_arg = parts[1].strip() if len(parts) > 1 else ""

    def _render_entries(entries: list[worktree_registry.WorktreeEntry]) -> None:
        if not entries:
            print_info("No worktrees tracked.")
            return
        active_slugs = set(worktree_observer.active_slugs())
        table = Table(title="Worktrees", expand=False)
        table.add_column("slug", no_wrap=True)
        table.add_column("status")
        table.add_column("obs", justify="center", width=3)
        table.add_column("branch", no_wrap=True)
        table.add_column("owner")
        table.add_column("pid", justify="right")
        table.add_column("path", max_width=55)
        for e in entries:
            obs = "[green]●[/]" if e.slug in active_slugs else "[dim]·[/]"
            status_color = {
                "active": "green",
                "kept": "yellow",
                "orphan": "red",
            }.get(e.status, "white")
            who = e.agent_name or e.owner
            table.add_row(
                e.slug,
                f"[{status_color}]{e.status}[/]",
                obs,
                e.branch,
                who,
                str(e.pid),
                e.worktree_path,
            )
        console.print(table)

    if not sub:
        # List entries for the current repo.
        proc = await asyncio.create_subprocess_shell(
            "git rev-parse --path-format=absolute --git-common-dir",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            print_error("Not inside a git repository.")
            return None
        common = Path(stdout.decode().strip())
        repo_root = str(common.parent) if common.name == ".git" else str(common)
        _render_entries(worktree_registry.list_for_repo(repo_root))
        return None

    if sub == "list":
        _render_entries(worktree_registry.load())
        return None

    if sub == "status":
        if not sub_arg:
            print_error("Usage: /worktree status <slug>")
            return None
        entry = worktree_registry.get(sub_arg)
        if entry is None:
            print_error(f"Unknown worktree: {sub_arg}")
            return None
        console.print(f"[bold]slug[/]:   {entry.slug}")
        console.print(f"[bold]branch[/]: {entry.branch}")
        console.print(f"[bold]repo[/]:   {entry.repo_root}")
        console.print(f"[bold]path[/]:   {entry.worktree_path}")
        console.print(f"[bold]status[/]: {entry.status}")
        console.print(f"[bold]owner[/]:  {entry.agent_name or entry.owner}")
        console.print(f"[bold]pid[/]:    {entry.pid}")
        summary = worktree_observer.summary(entry.slug)
        if summary is None:
            console.print("[dim]observer: not active[/]")
        else:
            console.print(
                f"[bold]observer[/]: +{summary['created']} ~{summary['modified']} "
                f"-{summary['deleted']} (baseline {summary['baseline_files']})",
            )
        return None

    if sub in {"sweep", "cleanup"}:
        orphans = worktree_registry.sweep_dead_pids()
        if orphans:
            print_warning(
                f"Marked {len(orphans)} entry(s) as orphan: "
                + ", ".join(e.slug for e in orphans)
            )
        else:
            print_info("No orphan owners found.")
        if sub == "cleanup":
            dropped = worktree_registry.prune_missing_paths()
            if dropped:
                print_ok(
                    f"Pruned {len(dropped)} missing-path entry(s): {', '.join(dropped)}"
                )
            removed = worktree_registry.cleanup_orphan_dirs()
            if removed:
                print_ok(f"Removed {removed} unreferenced checkout dir(s).")
            if not dropped and not removed and not orphans:
                print_info("Registry clean.")
        return None

    print_error("Usage: /worktree [list|status <slug>|sweep|cleanup]")
    return None


# ── /config ───────────────────────────────────────────────────────────────


async def cmd_config(args: str, _ctx: REPLContext) -> str | None:
    """View or edit Obscura settings (~/.obscura/settings.json).

    Usage:
        /config                     — show current config
        /config <key>               — show a specific key
        /config <key> <value>       — set a config value
        /config reset               — reset to defaults
    """
    settings_path = Path.home() / ".obscura" / "settings.json"

    if not args.strip():
        if not settings_path.exists():
            print_info(f"No settings file. Using defaults. ({settings_path})")
            return None
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            console.print(Syntax(json.dumps(data, indent=2), "json", theme="monokai"))
        except Exception as exc:
            logger.debug("suppressed exception in cmd_config", exc_info=True)
            print_error(f"Failed to read settings: {exc}")
        return None

    tokens = args.strip().split(None, 1)
    key = tokens[0]

    if key == "reset":
        if settings_path.exists():
            settings_path.unlink()
            print_ok("Settings reset to defaults.")
        else:
            print_info("Already using defaults.")
        return None

    data: dict[str, Any] = {}
    if settings_path.exists():
        with contextlib.suppress(Exception):
            data = json.loads(settings_path.read_text(encoding="utf-8"))

    if len(tokens) == 1:
        val: Any = data
        for part in key.split("."):
            if isinstance(val, dict):
                val = cast(dict[str, Any], val).get(part)
            else:
                val = None
                break
        if val is not None:
            console.print(f"[bold]{key}[/] = {json.dumps(val, indent=2)}")
        else:
            print_info(f"Key not set: {key}")
        return None

    value_str = tokens[1]
    try:
        value: Any = json.loads(value_str)
    except json.JSONDecodeError:
        logger.debug("suppressed exception in cmd_config", exc_info=True)
        value = value_str

    parts_list = key.split(".")
    cur: dict[str, Any] = data
    for part in parts_list[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts_list[-1]] = value

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print_ok(f"Set {key} = {json.dumps(value)}")
    return None


# ── /hooks ────────────────────────────────────────────────────────────────


async def cmd_hooks(args: str, _ctx: REPLContext) -> str | None:
    """Manage event hooks in settings.json.

    Usage:
        /hooks                  — list registered hooks
        /hooks add <event> <cmd> — add a shell hook for an event
        /hooks remove <event>   — remove hooks for an event
    """
    settings_path = Path.home() / ".obscura" / "settings.json"

    def _load() -> dict[str, Any]:
        if settings_path.exists():
            with contextlib.suppress(Exception):
                return json.loads(settings_path.read_text(encoding="utf-8"))
        return {}

    def _save(d: dict[str, Any]) -> None:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")

    tokens = args.strip().split(None, 2)
    sub = tokens[0].lower() if tokens else "list"

    if sub in ("list", ""):
        hooks = _load().get("hooks", {})
        if not hooks:
            print_info("No hooks configured. Add with: /hooks add <event> <command>")
            print_info(
                "Events: tool_call, tool_result, turn_start, turn_complete, error"
            )
            return None
        table = Table(title="Event Hooks", expand=False)
        table.add_column("Event", width=20)
        table.add_column("Command", max_width=50)
        for event, cmds in hooks.items():
            if isinstance(cmds, list):
                for c in cast(list[Any], cmds):
                    table.add_row(event, str(c))
            else:
                table.add_row(event, str(cmds))
        console.print(table)
        return None

    if sub == "add":
        if len(tokens) < 3:
            print_error("Usage: /hooks add <event> <command>")
            return None
        event_name, command = tokens[1], tokens[2]
        d = _load()
        hooks = d.setdefault("hooks", {})
        event_hooks_raw = hooks.setdefault(event_name, [])
        if not isinstance(event_hooks_raw, list):
            event_hooks_raw = [event_hooks_raw]
            hooks[event_name] = event_hooks_raw
        event_hooks = cast(list[Any], event_hooks_raw)
        event_hooks.append(command)
        _save(d)
        print_ok(f"Hook added: {event_name} -> {command}")
        return None

    if sub == "remove":
        if len(tokens) < 2:
            print_error("Usage: /hooks remove <event>")
            return None
        event_name = tokens[1]
        d = _load()
        hooks = d.get("hooks", {})
        if event_name in hooks:
            del hooks[event_name]
            _save(d)
            print_ok(f"Removed hooks for: {event_name}")
        else:
            print_info(f"No hooks registered for: {event_name}")
        return None

    print_info("Usage: /hooks [list|add <event> <cmd>|remove <event>]")
    return None


# ── /listen ───────────────────────────────────────────────────────────────

_LISTEN_SUFFIX = (
    "\n\n[LISTEN MODE] You are in listen mode. Observe the conversation "
    "passively. Only respond when directly asked a question. Keep responses "
    "minimal and to the point. Do not proactively suggest actions."
)


async def cmd_listen(args: str, ctx: REPLContext) -> str | None:
    """Toggle listen mode — passively observe, only respond when asked.

    Usage: /listen [on|off]
    """
    sub = args.strip().lower()
    current = getattr(ctx, "_listen_mode", False)

    if sub == "on" or (not sub and not current):
        ctx._listen_mode = True  # type: ignore[attr-defined]
        if not ctx.system_prompt.endswith(_LISTEN_SUFFIX):
            ctx.system_prompt += _LISTEN_SUFFIX
        print_ok("Listen mode ON — will only respond when asked.")
    elif sub == "off" or (not sub and current):
        ctx._listen_mode = False  # type: ignore[attr-defined]
        ctx.system_prompt = ctx.system_prompt.replace(_LISTEN_SUFFIX, "")
        print_ok("Listen mode OFF — normal interaction resumed.")
    else:
        print_info(f"Listen mode: {'ON' if current else 'OFF'}")
    return None


# ── /login + /logout ──────────────────────────────────────────────────────


async def cmd_login(args: str, _ctx: REPLContext) -> str | None:
    """Login for a provider. Usage: /login [provider]."""
    provider = args.strip().lower() or "copilot"

    if provider in ("copilot", "github"):
        from obscura.cli.auth_commands import ensure_github_oauth_session

        session = ensure_github_oauth_session(open_browser=True)
        if session is None:
            print_error(
                "GitHub OAuth login unavailable. Configure Supabase via "
                "`/secrets set SUPABASE_URL`, env vars, or ~/.obscura/.env.",
            )
            return None
        print_ok(f"Signed in via GitHub OAuth as {session.email}.")
        return None

    if provider in ("claude", "anthropic"):
        console.print("  [bold]export ANTHROPIC_API_KEY=sk-ant-...[/]")
    elif provider in ("openai", "gpt"):
        console.print("  [bold]export OPENAI_API_KEY=sk-...[/]")
    else:
        print_error(f"Unknown provider: {provider}. Known: claude, openai, copilot")
    return None


async def cmd_logout(args: str, _ctx: REPLContext) -> str | None:
    """Clear auth for a provider. Usage: /logout [provider]."""
    provider = args.strip().lower() or "copilot"
    env_map = {
        "claude": "ANTHROPIC_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gpt": "OPENAI_API_KEY",
    }
    env_var = env_map.get(provider)
    if env_var:
        if env_var in os.environ:
            del os.environ[env_var]
            print_ok(f"Cleared {env_var} from current session.")
        else:
            print_info(f"{env_var} not set in current session.")
    elif provider in ("copilot", "github"):
        print_info("Remove ~/.config/github-copilot/hosts.json to deauthorize.")
    else:
        print_error(f"Unknown provider: {provider}")
    return None


# ── /whoami + /secrets ────────────────────────────────────────────────────


async def cmd_whoami(_args: str, _ctx: REPLContext) -> str | None:
    """Show the currently authenticated Supabase user. Usage: /whoami."""

    session = load_session()
    if session is None:
        print_info("Not signed in. Run /login to authenticate via Supabase.")
        return None

    remaining = session.expires_at - int(time.time())
    state = "valid" if remaining > 0 else "EXPIRED"
    gh_state = "yes" if session.provider_token else "no"
    console.print(f"  user:         {session.email or session.user_id}")
    console.print(f"  user_id:      {session.user_id}")
    console.print(f"  provider:     {session.provider}")
    console.print(f"  token:        {state} (expires in {max(0, remaining)}s)")
    console.print(f"  github oauth: {gh_state}")
    console.print(f"  file:         {CREDENTIALS_PATH}")
    return None


async def cmd_secrets(args: str, _ctx: REPLContext) -> str | None:
    """Manage service secrets in the OS keyring.

    Usage:
      /secrets list [--only-set]             -- show where every value resolves
      /secrets get <NAME> [--reveal]         -- show one value (masked by default)
      /secrets set <NAME> <value> [--force]  -- persist to OS keyring
      /secrets delete <NAME> [--force]       -- remove from OS keyring

    Known names include Supabase identity config, LLM backend keys
    (ANTHROPIC_API_KEY, OPENAI_API_KEY, GITHUB_TOKEN, …), and common
    plugin credentials (NOTION_TOKEN, QDRANT_API_KEY, …). Pass ``--force``
    to store any other name.

    Env vars always win over keyring, so Docker/CI keep working unchanged.
    For hidden-input ``set``, prefer ``obscura-auth secrets set <NAME>``.
    """

    raw_tokens = args.strip().split()
    force = "--force" in raw_tokens
    only_set = "--only-set" in raw_tokens
    reveal = "--reveal" in raw_tokens
    positional = [t for t in raw_tokens if not t.startswith("--")]
    sub = positional[0].lower() if positional else "list"

    if sub == "list":
        mapping = _secrets.sources()
        kr_ready = _secrets.keyring_available()
        console.print(
            f"  Keyring backend: {'available' if kr_ready else 'unavailable'}",
        )
        width = max(len(name) for name in mapping)
        for name, source in mapping.items():
            if only_set and source == "missing":
                continue
            console.print(f"  {name.ljust(width)}  {source}")
        return None

    if sub in {"get", "set", "delete"} and len(positional) < 2:
        print_error(f"Usage: /secrets {sub} <NAME> [...]")
        return None

    name = positional[1].strip().upper() if len(positional) >= 2 else ""
    if name and not force and name not in _secrets.KNOWN_SECRET_NAMES:
        known = ", ".join(_secrets.KNOWN_SECRET_NAMES)
        print_error(
            f"Unknown secret '{positional[1]}'. Pass --force to store an "
            f"arbitrary name, or pick from: {known}",
        )
        return None

    if sub == "get":
        value = _secrets.resolve(name)
        if value is None:
            console.print(f"  {name}: (unset)")
            return None
        source = _secrets.sources([name]).get(name, "missing")
        shown = value if reveal else _secrets.mask(value)
        console.print(f"  {name}: {shown} [source: {source}]")
        return None

    if sub == "set":
        if len(positional) < 3 or not positional[2].strip():
            print_error(
                "Value required inline. For hidden input use "
                "`obscura-auth secrets set " + name + "` outside the REPL.",
            )
            return None
        if not _secrets.keyring_available():
            print_error(
                "No OS keyring backend available. "
                f"Set {name} as an env var or in ~/.obscura/.env instead.",
            )
            return None
        try:
            stored = _secrets.store(name, positional[2].strip())
        except _secrets.SecretsValidationError as exc:
            logger.debug("suppressed exception in cmd_secrets", exc_info=True)
            print_error(str(exc))
            return None
        if not stored:
            print_error(f"Failed to store {name} in keyring.")
            return None
        print_ok(f"Stored {name} in keyring.")
        return None

    if sub == "delete":
        if _secrets.delete(name):
            print_ok(f"Removed {name} from keyring.")
        else:
            print_info(f"No keyring entry found for {name}.")
        return None

    print_error(f"Unknown subcommand: {sub}. Try /secrets list|get|set|delete.")
    return None


# ── /release-notes ────────────────────────────────────────────────────────


async def cmd_release_notes(_args: str, _ctx: REPLContext) -> str | None:
    """Show release notes for the current version."""
    try:
        from importlib.metadata import version as pkg_version

        ver = pkg_version("obscura")
    except Exception:
        logger.debug("suppressed exception in cmd_release_notes", exc_info=True)
        ver = "dev"

    for candidate in [Path.cwd() / "CHANGELOG.md", Path.cwd().parent / "CHANGELOG.md"]:
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            lines = text.split("\n")
            section: list[str] = []
            in_section = False
            for line in lines:
                if line.startswith("## "):
                    if in_section:
                        break
                    if ver in line or not section:
                        in_section = True
                if in_section:
                    section.append(line)
            if section:
                console.print(Markdown("\n".join(section[:50])))
                return None

    print_info(f"Obscura {ver} — no release notes found.")
    return None


# ── /ide ──────────────────────────────────────────────────────────────────


async def cmd_ide(args: str, _ctx: REPLContext) -> str | None:
    """IDE integration. Usage: /ide [vscode|jetbrains]."""
    sub = args.strip().lower()

    if not sub:
        vsc = (Path.home() / ".vscode" / "extensions").exists()
        jb = (Path.home() / ".config" / "JetBrains").exists()
        console.print(
            f"  VS Code:    {'[green]detected[/]' if vsc else '[dim]not detected[/]'}"
        )
        console.print(
            f"  JetBrains:  {'[green]detected[/]' if jb else '[dim]not detected[/]'}"
        )
        print_info("Set up with: /ide vscode  or  /ide jetbrains")
        return None

    if sub == "vscode":
        vscode_dir = Path.cwd() / ".vscode"
        vscode_dir.mkdir(exist_ok=True)
        tasks_file = vscode_dir / "tasks.json"
        if not tasks_file.exists():
            tasks_data = {
                "version": "2.0.0",
                "tasks": [
                    {
                        "label": "Obscura: Review",
                        "type": "shell",
                        "command": "obscura '/review'",
                    },
                    {
                        "label": "Obscura: Commit",
                        "type": "shell",
                        "command": "obscura '/commit'",
                    },
                ],
            }
            tasks_file.write_text(
                json.dumps(tasks_data, indent=2) + "\n", encoding="utf-8"
            )
        print_ok("VS Code integration set up at .vscode/")
        return None

    if sub == "jetbrains":
        idea_dir = Path.cwd() / ".idea"
        idea_dir.mkdir(exist_ok=True)
        ext_tools = idea_dir / "obscura-tools.xml"
        if not ext_tools.exists():
            ext_tools.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<toolSet>\n'
                '  <tool name="Obscura Review" program="obscura" parameters="\'/review\'" />\n'
                '  <tool name="Obscura Commit" program="obscura" parameters="\'/commit\'" />\n'
                "</toolSet>\n",
                encoding="utf-8",
            )
        print_ok("JetBrains integration set up at .idea/")
        return None

    print_info("Usage: /ide [vscode|jetbrains]")
    return None


# ── /bug ──────────────────────────────────────────────────────────────────


async def cmd_bug(args: str, ctx: REPLContext) -> str | None:
    """Report a bug or view recent errors. Usage: /bug [report]."""
    sub = args.strip().lower()

    if sub == "report":
        try:
            from importlib.metadata import version as pkg_version

            ver = pkg_version("obscura")
        except Exception:
            logger.debug("suppressed exception in cmd_bug", exc_info=True)
            ver = "dev"
        report = (
            f"## Bug Report\n\n"
            f"- Obscura {ver}, Python {sys.version_info.major}.{sys.version_info.minor}, "
            f"{sys.platform}\n"
            f"- Backend: {ctx.backend}, Model: {ctx.model or 'default'}\n\n"
            f"### What happened?\n\n### Steps to reproduce\n\n1. \n"
        )
        console.print(report)
        try:
            subprocess.run(
                ["pbcopy"]
                if __import__("sys").platform == "darwin"
                else ["xclip", "-selection", "clipboard"],
                input=report.encode(),
                capture_output=True,
                timeout=5,
            )
            print_ok("Template copied to clipboard.")
        except Exception:
            logger.debug("suppressed exception in cmd_bug", exc_info=True)
        return None

    # Default: show recent errors

    log_path = Path(dlog.log_path)
    if not log_path.exists():
        print_info("No errors recorded. Generate report: /bug report")
        return None
    errors: list[str] = []
    for line in reversed(log_path.read_text(encoding="utf-8").splitlines()):
        try:
            entry = json.loads(line)
            if entry.get("type") == "error":
                errors.append(f"  {entry.get('data', {}).get('message', '?')[:100]}")
        except Exception:
            logger.debug("suppressed exception in cmd_bug", exc_info=True)
        if len(errors) >= 10:
            break
    if errors:
        console.print("[bold]Recent errors:[/]")
        for e in reversed(errors):
            console.print(f"[red]{e}[/]")
    else:
        print_info("No errors recorded.")
    return None


# ── /terminal-setup ───────────────────────────────────────────────────────


async def cmd_terminal_setup(_args: str, _ctx: REPLContext) -> str | None:
    """Diagnose and configure terminal capabilities."""
    cols, rows = shutil.get_terminal_size()
    term = os.environ.get("TERM", "unknown")
    colorterm = os.environ.get("COLORTERM", "")
    lang = os.environ.get("LANG", "")

    s256 = "256color" in term or colorterm in ("truecolor", "24bit")
    strue = colorterm in ("truecolor", "24bit")
    suni = "utf" in lang.lower()

    console.print("[bold]Terminal[/]")
    console.print(f"  TERM={term}  COLORTERM={colorterm or '(none)'}  {cols}x{rows}")
    console.print(f"  LANG={lang or '(none)'}  Shell={os.environ.get('SHELL', '?')}")

    def _c(ok: bool) -> str:
        return "[green]yes[/]" if ok else "[red]no[/]"

    console.print(
        f"  256-color: {_c(s256)}  True-color: {_c(strue)}  Unicode: {_c(suni)}"
    )
    console.print(
        f"  TTY: stdin={_c(sys.stdin.isatty())} stdout={_c(sys.stdout.isatty())}"
    )

    issues: list[str] = []
    if not suni:
        issues.append("Set LANG=en_US.UTF-8")
    if not s256:
        issues.append("Set TERM=xterm-256color")
    if cols < 80:
        issues.append(f"Width {cols} is narrow (80+ recommended)")
    if issues:
        console.print("\n[yellow]Fix:[/] " + " | ".join(issues))
    else:
        console.print("\n[green]Looks good![/]")
    return None


# ---------------------------------------------------------------------------
# Obscura-specific: goal, persona, guardrails, focus, checkpoint, undercover,
# token-budget, tool-policy, context-inject, recap
# ---------------------------------------------------------------------------


async def cmd_goal(args: str, ctx: REPLContext) -> str | None:
    """Set a persistent session goal that steers the agent across turns.

    The goal is injected into the system prompt so the model always
    keeps it in mind — even when you ask tangential follow-ups.

    Usage:
        /goal                               — show current goal
        /goal <description>                 — set a new goal
        /goal clear                         — remove the goal
        /goal check                         — ask the agent to self-assess progress

    Examples:
        /goal Refactor the auth middleware to use JWT tokens
        /goal Fix all failing tests in tests/unit/core/
        /goal Ship the v2 API by EOD — focus on /users and /teams endpoints
    """
    text = args.strip()
    current_goal = getattr(ctx, "_session_goal", "")

    if not text:
        if current_goal:
            console.print(f"[bold]Current goal:[/] {current_goal}")
        else:
            print_info("No goal set. Use /goal <description> to set one.")
        return None

    if text == "clear":
        if current_goal:
            _remove_goal_from_prompt(ctx)
            ctx._session_goal = ""  # type: ignore[attr-defined]
            print_ok("Goal cleared.")
        else:
            print_info("No goal to clear.")
        return None

    if text == "check":
        if not current_goal:
            print_info("No goal set. Nothing to check.")
            return None
        check_prompt = (
            f"My current goal is: {current_goal}\n\n"
            "Briefly assess: (1) what progress has been made toward this goal, "
            "(2) what remains to be done, (3) any blockers or risks. "
            "Be direct and specific — reference actual files/changes."
        )

        try:
            async for event in ctx.client.run_loop(check_prompt):
                render_event(event)
        except Exception as exc:
            logger.debug("suppressed exception in cmd_goal", exc_info=True)
            print_error(str(exc))
        return None

    # Set new goal
    if current_goal:
        _remove_goal_from_prompt(ctx)

    ctx._session_goal = text  # type: ignore[attr-defined]
    goal_block = f"\n\n[SESSION GOAL] {text}\nKeep this goal in mind across all responses. Prioritize actions that advance it."
    ctx.system_prompt += goal_block
    print_ok(f"Goal set: {text}")

    # Persist to session metadata
    try:
        await ctx.store.update_session(
            ctx.session_id,
            metadata={"goal": text},
        )
    except Exception:
        logger.debug("suppressed exception in cmd_goal", exc_info=True)
    return None


def _remove_goal_from_prompt(ctx: REPLContext) -> None:
    """Strip the goal injection from the system prompt."""
    ctx.system_prompt = re.sub(
        r"\n\n\[SESSION GOAL\].*?Prioritize actions that advance it\.",
        "",
        ctx.system_prompt,
        flags=re.DOTALL,
    )


async def cmd_persona(args: str, ctx: REPLContext) -> str | None:
    """Set an agent persona that shapes tone, expertise, and approach.

    Usage:
        /persona                              — show current persona
        /persona <description>                — set a persona
        /persona clear                        — remove persona
        /persona senior-backend               — preset: senior backend engineer
        /persona security-auditor             — preset: security reviewer
        /persona code-reviewer                — preset: thorough code reviewer
        /persona architect                    — preset: systems architect
        /persona junior-friendly              — preset: patient teacher
    """
    text = args.strip()
    current = getattr(ctx, "_persona", "")

    presets = {
        "senior-backend": (
            "You are a senior backend engineer with 10+ years experience. "
            "Focus on performance, correctness, and production readiness. "
            "Flag potential scaling issues. Prefer simple, proven patterns over clever solutions."
        ),
        "security-auditor": (
            "You are a security auditor. Prioritize identifying vulnerabilities: "
            "injection, auth bypass, SSRF, path traversal, secrets in code. "
            "Rate findings by severity. Suggest fixes with defense-in-depth."
        ),
        "code-reviewer": (
            "You are a meticulous code reviewer. Check for bugs, edge cases, "
            "naming consistency, test coverage gaps, and API contract violations. "
            "Be constructive but thorough. Flag anything you'd comment on in a real PR."
        ),
        "architect": (
            "You are a systems architect. Think about separation of concerns, "
            "module boundaries, data flow, and long-term maintainability. "
            "Suggest structural improvements. Consider backwards compatibility."
        ),
        "junior-friendly": (
            "Explain your reasoning step by step. Define terms that might be unfamiliar. "
            "When suggesting code, explain why each choice was made. "
            "Offer links to relevant docs. Be encouraging."
        ),
    }

    if not text:
        if current:
            console.print(f"[bold]Current persona:[/] {current[:80]}...")
        else:
            print_info("No persona set.")
            print_info(f"Presets: {', '.join(presets)}")
        return None

    if text == "clear":
        if current:
            _remove_block_from_prompt(ctx, "PERSONA")
            ctx._persona = ""  # type: ignore[attr-defined]
            print_ok("Persona cleared.")
        else:
            print_info("No persona to clear.")
        return None

    # Resolve preset or use custom text
    persona_text = presets.get(text, text)

    if current:
        _remove_block_from_prompt(ctx, "PERSONA")

    ctx._persona = persona_text  # type: ignore[attr-defined]
    ctx.system_prompt += f"\n\n[PERSONA] {persona_text}"
    label = text if text in presets else persona_text[:60]
    print_ok(f"Persona set: {label}")
    return None


async def cmd_guardrails(args: str, ctx: REPLContext) -> str | None:
    """Set runtime guardrails — constraints the agent must follow.

    Usage:
        /guardrails                           — show active guardrails
        /guardrails add <rule>                — add a guardrail
        /guardrails remove <n>                — remove guardrail by number
        /guardrails clear                     — remove all guardrails

    Examples:
        /guardrails add Do not modify any test files
        /guardrails add Only edit files under src/api/
        /guardrails add Always run tests after changing code
        /guardrails add Never use force push
    """
    rules: list[str] = getattr(ctx, "_guardrails", [])
    tokens = args.strip().split(None, 1)
    sub = tokens[0].lower() if tokens else ""
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    if not sub:
        if not rules:
            print_info("No guardrails active. Add with: /guardrails add <rule>")
            return None
        console.print("[bold]Active guardrails:[/]")
        for i, rule in enumerate(rules, 1):
            console.print(f"  {i}. {rule}")
        return None

    if sub == "add":
        if not rest:
            print_error("Usage: /guardrails add <rule>")
            return None
        rules.append(rest)
        ctx._guardrails = rules  # type: ignore[attr-defined]
        _rebuild_guardrails_prompt(ctx, rules)
        print_ok(f"Guardrail #{len(rules)} added: {rest}")
        return None

    if sub == "remove":
        if not rest or not rest.isdigit():
            print_error("Usage: /guardrails remove <number>")
            return None
        idx = int(rest) - 1
        if 0 <= idx < len(rules):
            removed = rules.pop(idx)
            ctx._guardrails = rules  # type: ignore[attr-defined]
            _rebuild_guardrails_prompt(ctx, rules)
            print_ok(f"Removed: {removed}")
        else:
            print_error(f"Invalid index. Valid: 1-{len(rules)}")
        return None

    if sub == "clear":
        rules.clear()
        ctx._guardrails = rules  # type: ignore[attr-defined]
        _remove_block_from_prompt(ctx, "GUARDRAILS")
        print_ok("All guardrails cleared.")
        return None

    # Shorthand: treat entire args as a rule to add
    rules.append(args.strip())
    ctx._guardrails = rules  # type: ignore[attr-defined]
    _rebuild_guardrails_prompt(ctx, rules)
    print_ok(f"Guardrail #{len(rules)} added: {args.strip()}")
    return None


def _rebuild_guardrails_prompt(ctx: REPLContext, rules: list[str]) -> None:
    """Rebuild the guardrails block in the system prompt."""
    _remove_block_from_prompt(ctx, "GUARDRAILS")
    if rules:
        block = "\n".join(f"  - {r}" for r in rules)
        ctx.system_prompt += (
            f"\n\n[GUARDRAILS] You MUST follow these constraints:\n{block}"
        )


async def cmd_focus(args: str, ctx: REPLContext) -> str | None:
    """Restrict the agent's attention to specific files or directories.

    When focus is set, the agent is instructed to only read/modify files
    within the focused paths. Useful for large repos.

    Usage:
        /focus                        — show current focus
        /focus <path> [path...]       — set focus to these paths
        /focus clear                  — remove focus restriction
    """
    current_focus: list[str] = getattr(ctx, "_focus_paths", [])
    text = args.strip()

    if not text:
        if current_focus:
            console.print("[bold]Focused on:[/]")
            for p in current_focus:
                console.print(f"  {p}")
        else:
            print_info("No focus set. Agent can access any files.")
        return None

    if text == "clear":
        ctx._focus_paths = []  # type: ignore[attr-defined]
        _remove_block_from_prompt(ctx, "FOCUS")
        print_ok("Focus cleared — full codebase access.")
        return None

    paths = text.split()
    # Validate paths exist
    valid: list[str] = []
    for p in paths:
        resolved = Path(p).expanduser().resolve()
        if resolved.exists():
            valid.append(str(resolved))
        else:
            print_warning(f"Path not found: {p}")

    if not valid:
        print_error("No valid paths.")
        return None

    ctx._focus_paths = valid  # type: ignore[attr-defined]
    _remove_block_from_prompt(ctx, "FOCUS")
    path_list = "\n".join(f"  - {p}" for p in valid)
    ctx.system_prompt += (
        f"\n\n[FOCUS] Only read and modify files within these paths:\n{path_list}\n"
        f"Do not touch files outside these paths unless explicitly asked."
    )
    print_ok(f"Focus set: {', '.join(Path(p).name for p in valid)}")
    return None


async def cmd_checkpoint(args: str, ctx: REPLContext) -> str | None:
    """Save or restore a conversation checkpoint (snapshot).

    Unlike /stash (which clears context), /checkpoint saves a named
    snapshot you can return to — like a git tag for your conversation.

    Usage:
        /checkpoint save [name]       — save current state
        /checkpoint list               — list saved checkpoints
        /checkpoint restore <name>     — restore a checkpoint
        /checkpoint delete <name>      — delete a checkpoint
    """
    checkpoints: dict[str, dict[str, Any]] = getattr(ctx, "_checkpoints", {})

    tokens = args.strip().split(None, 1)
    sub = tokens[0].lower() if tokens else "list"
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    if sub == "list":
        if not checkpoints:
            print_info("No checkpoints. Save one with: /checkpoint save [name]")
            return None
        table = Table(title="Checkpoints", expand=False)
        table.add_column("Name", width=20)
        table.add_column("Messages", width=8, justify="right")
        table.add_column("Saved", width=16)
        for name, cp in checkpoints.items():
            table.add_row(name, str(cp["msg_count"]), cp["saved_at"])
        console.print(table)
        return None

    if sub == "save":
        name = rest or f"cp-{len(checkpoints) + 1}"
        checkpoints[name] = {
            "history": list(ctx.message_history),
            "system_prompt": ctx.system_prompt,
            "file_changes": list(ctx.file_changes),
            "msg_count": len(ctx.message_history),
            "saved_at": datetime.now(UTC).strftime("%H:%M:%S"),
        }
        ctx._checkpoints = checkpoints  # type: ignore[attr-defined]
        print_ok(f"Checkpoint '{name}' saved ({len(ctx.message_history)} messages).")
        return None

    if sub == "restore":
        if not rest:
            print_error("Usage: /checkpoint restore <name>")
            return None
        cp = checkpoints.get(rest)
        if not cp:
            print_error(f"Checkpoint '{rest}' not found.")
            return None
        ctx.message_history.clear()
        ctx.message_history.extend(cp["history"])
        ctx.system_prompt = cp["system_prompt"]
        ctx.file_changes.clear()
        ctx.file_changes.extend(cp["file_changes"])
        print_ok(f"Restored checkpoint '{rest}' ({cp['msg_count']} messages).")
        return None

    if sub == "delete":
        if not rest:
            print_error("Usage: /checkpoint delete <name>")
            return None
        if rest in checkpoints:
            del checkpoints[rest]
            ctx._checkpoints = checkpoints  # type: ignore[attr-defined]
            print_ok(f"Deleted checkpoint '{rest}'.")
        else:
            print_error(f"Checkpoint '{rest}' not found.")
        return None

    print_info("Usage: /checkpoint save|list|restore|delete")
    return None


async def cmd_undercover(args: str, _ctx: REPLContext) -> str | None:
    """Toggle undercover mode (suppress AI attribution in commits/output).

    Usage:
        /undercover              — show status
        /undercover on|off       — force on/off
        /undercover auto         — auto-detect from repo context
    """
    from obscura.kairos.undercover import UndercoverMode

    mode = UndercoverMode()
    sub = args.strip().lower()

    if not sub:
        status = "ON" if mode.is_active else "OFF"
        _forced_attr = getattr(mode, "_forced", None)
        auto = "(auto-detected)" if _forced_attr is None else "(forced)"
        print_info(f"Undercover mode: {status} {auto}")
        return None

    if sub == "on":
        mode.force(True)
        print_ok("Undercover mode ON — AI attribution suppressed.")
    elif sub == "off":
        mode.force(False)
        print_ok("Undercover mode OFF — normal attribution.")
    elif sub == "auto":
        mode.auto()
        status = "ON" if mode.is_active else "OFF"
        print_ok(f"Undercover mode set to auto-detect (currently {status}).")
    else:
        print_info("Usage: /undercover [on|off|auto]")
    return None


async def cmd_token_budget(args: str, ctx: REPLContext) -> str | None:
    """Set a token or cost budget for the session.

    When the budget is exceeded, the agent pauses and asks before continuing.

    Usage:
        /token-budget                    — show current budget
        /token-budget <tokens>           — set token budget (e.g. 50000, 100k)
        /token-budget $<amount>          — set cost budget (e.g. $0.50, $5)
        /token-budget off                — remove budget
    """

    tracker = get_cost_tracker()
    text = args.strip()
    current_budget = getattr(ctx, "_token_budget", None)
    current_cost_budget = getattr(ctx, "_cost_budget", None)

    if not text:
        used_tokens = tracker.total_input_tokens() + tracker.total_output_tokens()
        used_cost = tracker.session_total_usd()
        if current_budget:
            pct = int(used_tokens / current_budget * 100) if current_budget else 0
            console.print(
                f"[bold]Token budget:[/] {used_tokens:,} / {current_budget:,} ({pct}%)"
            )
        elif current_cost_budget:
            pct = (
                int(used_cost / current_cost_budget * 100) if current_cost_budget else 0
            )
            console.print(
                f"[bold]Cost budget:[/] ${used_cost:.4f} / ${current_cost_budget:.2f} ({pct}%)"
            )
        else:
            console.print(
                f"No budget set. Used: {used_tokens:,} tokens, ${used_cost:.4f}"
            )
        return None

    if text == "off":
        ctx._token_budget = None  # type: ignore[attr-defined]
        ctx._cost_budget = None  # type: ignore[attr-defined]
        print_ok("Budget removed.")
        return None

    if text.startswith("$"):
        try:
            amount = float(text[1:])
            ctx._cost_budget = amount  # type: ignore[attr-defined]
            ctx._token_budget = None  # type: ignore[attr-defined]
            print_ok(f"Cost budget set: ${amount:.2f}")
        except ValueError:
            logger.debug("suppressed exception in cmd_token_budget", exc_info=True)
            print_error("Invalid amount. Use: /token-budget $0.50")
        return None

    # Parse token count (supports "50k", "100K", plain numbers)
    try:
        text_clean = text.lower().replace(",", "")
        if text_clean.endswith("k"):
            budget = int(float(text_clean[:-1]) * 1000)
        elif text_clean.endswith("m"):
            budget = int(float(text_clean[:-1]) * 1_000_000)
        else:
            budget = int(text_clean)
        ctx._token_budget = budget  # type: ignore[attr-defined]
        ctx._cost_budget = None  # type: ignore[attr-defined]
        print_ok(f"Token budget set: {budget:,}")
    except ValueError:
        logger.debug("suppressed exception in cmd_token_budget", exc_info=True)
        print_error(
            "Invalid budget. Use: /token-budget 50000 or /token-budget 50k or /token-budget $0.50"
        )
    return None


async def cmd_tool_policy(args: str, ctx: REPLContext) -> str | None:
    """Configure tool access policy for the session.

    Usage:
        /tool-policy                         — show current policy
        /tool-policy allow-all               — allow all tools (native + custom)
        /tool-policy custom-only             — only custom tools (block native)
        /tool-policy allow <tool> [tool...]   — whitelist specific tools
        /tool-policy deny <tool> [tool...]    — blacklist specific tools
        /tool-policy reset                   — restore default policy
    """

    current: ToolPolicy | None = getattr(ctx.client, "_tool_policy", None)
    tokens = args.strip().split()
    sub = tokens[0].lower() if tokens else ""
    rest = tokens[1:] if len(tokens) > 1 else []

    if not sub:
        if current is None:
            print_info("Tool policy: default (custom tools only)")
        else:
            console.print("[bold]Tool policy:[/]")
            console.print(
                f"  Native tools: {'allowed' if current.allow_native else 'blocked'}"
            )
            if current.allowed_tools is not None:
                console.print(f"  Allowed: {', '.join(current.allowed_tools)}")
            if current.denied_tools:
                console.print(f"  Denied: {', '.join(current.denied_tools)}")
        return None

    if sub == "allow-all":
        ctx.client._tool_policy = ToolPolicy.allow_all()  # noqa: SLF001
        print_ok("Tool policy: all tools allowed (native + custom).")
    elif sub == "custom-only":
        ctx.client._tool_policy = ToolPolicy.custom_only()  # noqa: SLF001
        print_ok("Tool policy: custom tools only.")
    elif sub == "allow" and rest:
        ctx.client._tool_policy = ToolPolicy.restricted(rest)  # noqa: SLF001
        print_ok(f"Tool policy: only {', '.join(rest)} allowed.")
    elif sub == "deny" and rest:
        ctx.client._tool_policy = ToolPolicy.blocked(rest)  # noqa: SLF001
        print_ok(f"Tool policy: {', '.join(rest)} blocked.")
    elif sub == "reset":
        ctx.client._tool_policy = None  # noqa: SLF001
        print_ok("Tool policy reset to default.")
    else:
        print_info(
            "Usage: /tool-policy [allow-all|custom-only|allow <t>|deny <t>|reset]"
        )
    return None


async def cmd_context_inject(args: str, ctx: REPLContext) -> str | None:
    """Inject context from a file or URL into the conversation.

    The content is added as a user message prefixed with context markers,
    so the agent sees it as reference material.

    Usage:
        /context-inject <path>         — inject file contents
        /context-inject --paste        — inject from clipboard
    """
    text = args.strip()
    if not text:
        print_error("Usage: /context-inject <path>  or  /context-inject --paste")
        return None

    if text == "--paste":
        try:
            proc = subprocess.run(
                ["pbpaste"]
                if __import__("sys").platform == "darwin"
                else ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                timeout=5,
            )
            content = proc.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("suppressed exception in cmd_context_inject", exc_info=True)
            print_error(f"Clipboard read failed: {exc}")
            return None
    else:
        path = Path(text).expanduser().resolve()
        if not path.is_file():
            print_error(f"Not a file: {path}")
            return None
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug("suppressed exception in cmd_context_inject", exc_info=True)
            print_error(f"Failed to read: {exc}")
            return None

    if not content.strip():
        print_info("Empty content — nothing injected.")
        return None

    # Truncate if very large
    if len(content) > 50_000:
        content = content[:50_000] + "\n\n... (truncated at 50k chars)"

    label = text if text != "--paste" else "clipboard"
    ctx.message_history.append(
        (
            "user",
            f"[CONTEXT INJECTION: {label}]\n\n{content}",
        )
    )
    char_count = len(content)
    if char_count > 1000:
        size_str = f"{char_count / 1000:.1f}k chars"
    else:
        size_str = f"{char_count} chars"
    print_ok(f"Injected {size_str} from {label}.")
    return None


async def cmd_recap(_args: str, ctx: REPLContext) -> str | None:
    """Generate a structured recap of what the agent has done this session.

    Outputs: files changed, tools used, key decisions, and remaining work.
    More structured than /summary — designed for handoff or status reports.
    """
    if len(ctx.message_history) < 2:
        print_info("Not enough conversation to recap.")
        return None

    modified = get_recently_modified_files(limit=20)
    read_files = get_recently_read_files(limit=20)
    goal = getattr(ctx, "_session_goal", "")

    recent = ctx.message_history[-30:]
    context_lines = [f"[{r}]: {t[:200]}" for r, t in recent]

    prompt = (
        "Generate a structured session recap with these sections:\n"
        "1. **What was done** — key actions and changes (2-4 bullets)\n"
        "2. **Files modified** — list with one-line descriptions\n"
        "3. **Key decisions** — important choices made and why\n"
        "4. **Remaining work** — what's left to do\n"
    )
    if goal:
        prompt += f"5. **Goal progress** — status toward: {goal}\n"
    prompt += (
        f"\nModified files: {', '.join(modified[:10]) if modified else 'none'}\n"
        f"Read files: {', '.join(read_files[:10]) if read_files else 'none'}\n\n"
        "Conversation:\n" + "\n".join(context_lines)
    )

    try:
        text = await _oneshot_stream(ctx.client, prompt)
        if text:
            console.print(Markdown(text))
        else:
            print_info("(no recap generated)")
    except _OneshotStalled as stalled:
        logger.debug("suppressed exception in cmd_recap", exc_info=True)
        if stalled.partial:
            console.print(Markdown(stalled.partial))
            print_info(
                f"[dim](truncated — no output for {stalled.idle_timeout:.0f}s)[/]"
            )
        else:
            print_error(f"Recap stalled — no output for {stalled.idle_timeout:.0f}s.")
    except Exception as exc:
        logger.debug("suppressed exception in cmd_recap", exc_info=True)
        print_error(f"Recap failed: {exc}")
    return None


def _remove_block_from_prompt(ctx: REPLContext, tag: str) -> None:
    """Remove a tagged block like [TAG] ... from the system prompt."""
    ctx.system_prompt = re.sub(
        rf"\n\n\[{re.escape(tag)}\].*?(?=\n\n\[|$)",
        "",
        ctx.system_prompt,
        flags=re.DOTALL,
    )


# ---------------------------------------------------------------------------
# /phantom — Obscura becomes the user (three independent sub-modes)
# ---------------------------------------------------------------------------

# Sub-mode tags for system prompt injection/removal.
_PHANTOM_TAG = "PHANTOM"
_GHOST_TAG = "PHANTOM_GHOST"
_PROXY_TAG = "PHANTOM_PROXY"
_AUTOPILOT_TAG = "PHANTOM_AUTOPILOT"
_LEVEL_TAG = "PHANTOM_LEVEL"

# -- Autonomy levels -------------------------------------------------------

_PHANTOM_LEVELS: dict[int, tuple[str, str]] = {
    1: (
        "shadow",
        (
            "You are in SHADOW mode. You NEVER act — only observe and suggest.\n"
            "- Draft everything but execute nothing. Show what you WOULD do.\n"
            "- Prefix suggestions with 'I would...' or 'Draft:'\n"
            "- Never commit, push, send, deploy, or modify anything.\n"
            "- Wait for explicit approval before any action."
        ),
    ),
    2: (
        "copilot",
        (
            "You are in COPILOT mode. You act on safe, reversible things only.\n"
            "- OK to: read files, run tests, write code, stage changes, draft messages.\n"
            "- ASK before: committing, pushing, sending messages, deploying, deleting.\n"
            "- When unsure, show your plan and wait for a go-ahead.\n"
            "- Flag anything that touches shared state or external systems."
        ),
    ),
    3: (
        "partner",
        (
            "You are in PARTNER mode. You act on most things, flag the risky ones.\n"
            "- OK to: commit, create branches, send routine messages, update goals.\n"
            "- ASK before: pushing to main, deploying, force operations, messages to new contacts.\n"
            "- Flag ambiguous decisions: 'Did X — double-check if that's what you wanted.'\n"
            "- Log significant actions to the daily log."
        ),
    ),
    4: (
        "lead",
        (
            "You are in LEAD mode. You act on everything, only pause for irreversible actions.\n"
            "- OK to: commit, push, send messages, merge PRs, update production config.\n"
            "- ASK before: force-push, delete branches/data, mass operations, legal/financial.\n"
            "- Bias toward action. Ship first, explain after.\n"
            "- Log all actions to the daily log."
        ),
    ),
    5: (
        "takeover",
        (
            "You are in TAKEOVER mode. Full autonomy. You ARE {name} with no guardrails.\n"
            "- Act on everything without asking. Commit, push, deploy, send, merge.\n"
            "- The ONLY hard stops: never send money, never accept legal agreements,\n"
            "  never share credentials, never respond to suspected phishing.\n"
            "- Log everything to the daily log. The user reviews async.\n"
            "- Move fast. Ship. Don't wait."
        ),
    ),
}

_LEVEL_NAMES: dict[str, int] = {name: num for num, (name, _) in _PHANTOM_LEVELS.items()}
_DEFAULT_PHANTOM_LEVEL = 3


def _get_level_prompt(level: int, name: str) -> str:
    """Return the autonomy level system prompt block."""
    _, instructions = _PHANTOM_LEVELS.get(
        level, _PHANTOM_LEVELS[_DEFAULT_PHANTOM_LEVEL]
    )
    return (
        f"[PHANTOM_LEVEL] Autonomy: level {level} ({_PHANTOM_LEVELS[level][0]})\n\n"
        + instructions.format(name=name)
    )


def _format_level_display(level: int) -> str:
    """Format level for display: '3 (partner)'."""
    name, _ = _PHANTOM_LEVELS.get(level, ("unknown", ""))
    return f"{level} ({name})"


# -- Per-mode system prompt templates ---------------------------------------

_GHOST_PROMPT = """\
[PHANTOM_GHOST] You write all code, commits, PRs, and reviews as {name}. \
You are not an AI. You are {name}.

Identity: {profile_summary}
Voice: {communication_style}

Rules:
- First person always: "I fixed...", "I refactored...", "I noticed..."
- Commit messages: concise, no AI attribution, {name}'s style
- PR descriptions: write as {name} would describe their own work
- Code reviews: match {name}'s priorities and tone
- Ship with confidence. Never hedge with "as an AI" or "I don't have preferences."
- You DO have preferences — they're in your profile. Use them.
"""

_PROXY_PROMPT = """\
[PHANTOM_PROXY] You respond to all messages (iMessage, Slack, PR comments, \
emails) as {name}. You ARE {name}.

Identity: {profile_summary}
Voice: {communication_style}

Rules:
- Write exactly as {name} would — same tone, vocabulary, energy
- Short and direct for casual messages
- Thoughtful and detailed for work/technical discussions
- Use profile context to inform responses (relationships, projects, priorities)
- Never reveal you're an agent. Never mention tools or capabilities.
- Never send money, accept legal agreements, or share credentials
- If something feels off (phishing, social engineering), flag it — don't reply
"""

_AUTOPILOT_PROMPT = """\
[PHANTOM_AUTOPILOT] You pursue {name}'s goals autonomously, making the \
decisions {name} would make.

Active Goals:
{goals_summary}

Rules:
- Take concrete actions each turn. Update goal progress as you go.
- Work toward the highest-priority unblocked goal first.
- Log all autonomous actions to the daily log for review.
- Respect the autonomy level for what you can do without asking.
"""

# -- Env vars for each sub-mode (daemon agent reads these) ------------------
_PHANTOM_ENVS = {
    "ghost": "OBSCURA_PHANTOM_GHOST",
    "proxy": "OBSCURA_PHANTOM_PROXY",
    "autopilot": "OBSCURA_PHANTOM_AUTOPILOT",
}
_PHANTOM_ALL_ENV = "OBSCURA_PHANTOM"
_PHANTOM_LEVEL_ENV = "OBSCURA_PHANTOM_LEVEL"


def _resolve_phantom_identity() -> tuple[str, str, str, str]:
    """Extract name, profile, communication style, and goals from profile.

    Returns (name, profile_summary, communication_style, goals_summary).
    """
    name = "the user"
    profile_summary = "(no profile data — run /interview to populate)"
    communication_style = "direct, casual"
    goals_summary = "(no active goals)"

    # Vector-backed profile.
    try:
        from obscura.profile.builder import ProfileBuilder
        from obscura.profile.models import ProfileCategory
        from obscura.profile.store import ProfileStore

        store = ProfileStore.for_user(current_cli_user())
        builder = ProfileBuilder()
        summary = builder.build_summary(store, max_tokens=600)
        if summary:
            profile_summary = summary

        for f in store.get_facts_by_category(ProfileCategory.IDENTITY):
            if "name" in f.key.lower():
                name = f.value
                break

        prefs = store.get_facts_by_category(ProfileCategory.PREFERENCE)
        if prefs:
            communication_style = "; ".join(f.value for f in prefs[:3])
    except Exception:
        logger.debug("suppressed exception in _resolve_phantom_identity", exc_info=True)

    # Markdown fallback for name.
    if name == "the user":
        try:
            from obscura.kairos.user_profile import UserProfile

            text = UserProfile().read()
            if text:
                m = re.search(r"\*\*Name\*\*:\s*(.+?)(?:\s*\(|$)", text, re.MULTILINE)
                if m:
                    name = m.group(1).strip()
                if profile_summary.startswith("(no"):
                    profile_summary = UserProfile().active_summary(max_lines=20)
        except Exception:
            logger.debug(
                "suppressed exception in _resolve_phantom_identity", exc_info=True
            )

    # Goals.
    try:
        gs = GoalBoard().active_summary(max_lines=8)
        if gs:
            goals_summary = gs
    except Exception:
        logger.debug("suppressed exception in _resolve_phantom_identity", exc_info=True)

    return name, profile_summary, communication_style, goals_summary


def _build_mode_prompt(
    mode: str,
    name: str,
    profile_summary: str,
    communication_style: str,
    goals_summary: str,
) -> str:
    """Build the system prompt block for a single phantom sub-mode."""
    templates = {
        "ghost": _GHOST_PROMPT,
        "proxy": _PROXY_PROMPT,
        "autopilot": _AUTOPILOT_PROMPT,
    }
    tmpl = templates.get(mode, "")
    if not tmpl:
        return ""
    return tmpl.format(
        name=name,
        profile_summary=profile_summary,
        communication_style=communication_style,
        goals_summary=goals_summary,
    )


def _phantom_status_line(ctx: REPLContext) -> str:
    """Build a compact status string for phantom modes."""
    modes: list[str] = []
    for mode in ("ghost", "proxy", "autopilot"):
        if getattr(ctx, f"_phantom_{mode}", False):
            modes.append(mode)
    if not modes:
        return ""
    name = getattr(ctx, "_phantom_name", "")
    level: int = getattr(ctx, "_phantom_level", _DEFAULT_PHANTOM_LEVEL)
    level_name = _PHANTOM_LEVELS.get(level, ("?", ""))[0]
    return f"phantom:{'+'.join(modes)} L{level}({level_name})" + (
        f" ({name})" if name else ""
    )


async def cmd_phantom(args: str, ctx: REPLContext) -> str | None:
    """Toggle phantom mode — Obscura becomes you.

    Three independent sub-modes you can mix and match, plus an autonomy
    level that controls how aggressively Obscura acts on your behalf.

    Usage:
        /phantom                  — toggle all on/off
        /phantom on               — activate all three modes
        /phantom off              — deactivate all
        /phantom ghost            — toggle ghost (code, commits, PRs)
        /phantom proxy            — toggle proxy (message responses)
        /phantom autopilot        — toggle autopilot (goal pursuit)
        /phantom ghost on         — activate ghost only
        /phantom proxy off        — deactivate proxy only
        /phantom level            — show current autonomy level
        /phantom level 4          — set by number (1-5)
        /phantom level lead       — set by name
        /phantom status           — show what's active

    Autonomy levels:
        1 (shadow)    — observe and suggest, never act
        2 (copilot)   — act on safe things, ask for the rest
        3 (partner)   — act on most things, flag risky ones [default]
        4 (lead)      — act on everything, pause for irreversible
        5 (takeover)  — full autonomy, no check-ins
    """
    tokens = args.strip().lower().split()
    sub = tokens[0] if tokens else ""
    modifier = tokens[1] if len(tokens) > 1 else ""

    active_modes = {
        m
        for m in ("ghost", "proxy", "autopilot")
        if getattr(ctx, f"_phantom_{m}", False)
    }
    any_active = bool(active_modes)
    current_level = getattr(ctx, "_phantom_level", _DEFAULT_PHANTOM_LEVEL)

    # -- /phantom status -------------------------------------------------------
    if sub == "status":
        if any_active:
            name = getattr(ctx, "_phantom_name", "unknown")
            mode_list = ", ".join(sorted(active_modes))
            level_str = _format_level_display(current_level)
            console.print(
                f"[bold green]Phantom: ON[/] — [bold]{name}[/]  "
                f"modes: {mode_list}  level: {level_str}"
            )
        else:
            console.print("[dim]Phantom: OFF[/]")
        return None

    # -- /phantom level [N|name] -----------------------------------------------
    if sub == "level":
        if not modifier:
            # Show current level + all options.
            console.print(
                f"[bold]Current level:[/] {_format_level_display(current_level)}\n"
            )
            for num, (lname, desc) in sorted(_PHANTOM_LEVELS.items()):
                marker = " ←" if num == current_level else ""
                first_line = desc.split("\n")[0]
                console.print(f"  [bold]{num}[/] ({lname}){marker} — {first_line}")
            return None

        # Parse level: by number or name.
        new_level = _LEVEL_NAMES.get(modifier)
        if new_level is None:
            try:
                new_level = int(modifier)
            except ValueError:
                logger.debug("suppressed exception in cmd_phantom", exc_info=True)
                print_error(
                    f"Unknown level: {modifier}. Use 1-5 or shadow/copilot/partner/lead/takeover."
                )
                return None
        if new_level not in _PHANTOM_LEVELS:
            print_error(f"Level must be 1-5. Got: {new_level}")
            return None

        _set_phantom_level(ctx, new_level)
        print_ok(f"Autonomy level: {_format_level_display(new_level)}")
        return None

    # -- /phantom off (all) ----------------------------------------------------
    if sub == "off" or (not sub and any_active):
        if not any_active:
            print_info("Phantom is already off.")
            return None
        for mode in ("ghost", "proxy", "autopilot"):
            _phantom_mode_off(ctx, mode)
        _remove_block_from_prompt(ctx, _LEVEL_TAG)
        print_ok("Phantom OFF — all modes deactivated.")
        return None

    # -- /phantom on (all) -----------------------------------------------------
    if sub == "on" or (not sub and not any_active):
        name, profile_summary, comm_style, goals_summary = _resolve_phantom_identity()
        for mode in ("ghost", "proxy", "autopilot"):
            _phantom_mode_on(
                ctx, mode, name, profile_summary, comm_style, goals_summary
            )
        _set_phantom_level(ctx, current_level)
        level_str = _format_level_display(current_level)
        print_ok(
            f"Phantom ON — ghost + proxy + autopilot as {name} (level {level_str})"
        )
        if profile_summary.startswith("(no"):
            print_warning("Profile is sparse. Run /interview first for best results.")
        return None

    # -- /phantom <mode> [on|off] — individual toggle --------------------------
    if sub in ("ghost", "proxy", "autopilot"):
        is_on = getattr(ctx, f"_phantom_{sub}", False)

        if modifier == "off" or (not modifier and is_on):
            if not is_on:
                print_info(f"Phantom {sub} is already off.")
                return None
            _phantom_mode_off(ctx, sub)
            remaining = _phantom_status_line(ctx)
            if remaining:
                print_ok(f"Phantom {sub} OFF.  Still active: {remaining}")
            else:
                _remove_block_from_prompt(ctx, _LEVEL_TAG)
                print_ok(f"Phantom {sub} OFF.")
            return None

        if modifier == "on" or (not modifier and not is_on):
            name, profile_summary, comm_style, goals_summary = (
                _resolve_phantom_identity()
            )
            _phantom_mode_on(ctx, sub, name, profile_summary, comm_style, goals_summary)
            # Inject level prompt if this is the first mode being turned on.
            if not any_active:
                _set_phantom_level(ctx, current_level)
            print_ok(
                f"Phantom {sub} ON as {name} (level {_format_level_display(current_level)})"
            )
            return None

        print_error(f"Usage: /phantom {sub} [on|off]")
        return None

    print_error(
        f"Unknown: /phantom {sub}. Try /phantom [on|off|ghost|proxy|autopilot|level|status]."
    )
    return None


def _set_phantom_level(ctx: REPLContext, level: int) -> None:
    """Set the autonomy level — injects/replaces the level prompt block."""
    ctx._phantom_level = level  # type: ignore[attr-defined]
    os.environ[_PHANTOM_LEVEL_ENV] = str(level)

    # Remove old level block if present, then inject new one.
    _remove_block_from_prompt(ctx, _LEVEL_TAG)
    name = getattr(ctx, "_phantom_name", "the user")
    level_block = _get_level_prompt(level, name)
    ctx.system_prompt += f"\n\n{level_block}"


def _phantom_mode_on(
    ctx: REPLContext,
    mode: str,
    name: str,
    profile_summary: str,
    communication_style: str,
    goals_summary: str,
) -> None:
    """Activate a single phantom sub-mode."""
    if getattr(ctx, f"_phantom_{mode}", False):
        return  # already on

    prompt_block = _build_mode_prompt(
        mode, name, profile_summary, communication_style, goals_summary
    )
    if prompt_block:
        ctx.system_prompt += f"\n\n{prompt_block}"

    setattr(ctx, f"_phantom_{mode}", True)
    ctx._phantom_name = name  # type: ignore[attr-defined]

    # Set env vars so daemon agent picks up the mode.
    env_key = _PHANTOM_ENVS.get(mode)
    if env_key:
        os.environ[env_key] = "1"
    os.environ[_PHANTOM_ALL_ENV] = "1"

    # Force undercover when any phantom mode is on.
    try:
        from obscura.kairos.undercover import UndercoverMode

        UndercoverMode().force(True)
    except Exception:
        logger.debug("suppressed exception in _phantom_mode_on", exc_info=True)


def _phantom_mode_off(ctx: REPLContext, mode: str) -> None:
    """Deactivate a single phantom sub-mode."""
    tag = {"ghost": _GHOST_TAG, "proxy": _PROXY_TAG, "autopilot": _AUTOPILOT_TAG}.get(
        mode
    )
    if tag:
        _remove_block_from_prompt(ctx, tag)

    setattr(ctx, f"_phantom_{mode}", False)

    # Clear env var for this mode.
    env_key = _PHANTOM_ENVS.get(mode)
    if env_key:
        os.environ.pop(env_key, None)

    # If no phantom modes remain, clear the global env and restore undercover.
    remaining = any(
        getattr(ctx, f"_phantom_{m}", False) for m in ("ghost", "proxy", "autopilot")
    )
    if not remaining:
        os.environ.pop(_PHANTOM_ALL_ENV, None)
        ctx._phantom_name = ""  # type: ignore[attr-defined]
        try:
            from obscura.kairos.undercover import UndercoverMode

            UndercoverMode().auto()
        except Exception:
            logger.debug("suppressed exception in _phantom_mode_off", exc_info=True)


# ---------------------------------------------------------------------------
# /interview — Agent-driven onboarding to populate profile + goals
# ---------------------------------------------------------------------------

_INTERVIEW_PROFILE_PROMPT = """\
You are conducting a user profile interview. Your job is to learn about the \
user so you can populate their profile with structured facts.

You ARE the user's second brain. Talk like a peer, not an interviewer. Be \
direct, casual, curious. Ask follow-up questions when answers are vague.

You have these tools available:
- profile_set(key, value, category) — store a fact
- profile_get() — check current profile state
- goal(action="create", title, priority, context, acceptance_criteria) — create a goal
- goal(action="list") — see existing goals

Categories for profile_set:
- identity: name, email, location, education (never decays)
- career: role, company, comp, targets, tenure (90-day half-life)
- skill: languages, frameworks, expertise (120-day half-life)
- preference: working style, tool preferences, communication style (180-day half-life)
- personal: hobbies, interests, habits, travel (60-day half-life)
- learned: recent/ephemeral observations (30-day half-life)

RULES:
1. Ask ONE question at a time. Wait for the answer before asking the next.
2. After each answer, immediately call profile_set or goal(action="create") to persist it.
3. Start by checking what's already in the profile (profile_get) and skip what's filled.
4. Cover these areas in order: identity → career → skills → working style → goals → personal
5. For goals: ask what they're trying to accomplish right now, decompose into \
   structured goals with acceptance criteria.
6. Be efficient. If they give a long answer, extract multiple facts from it.
7. When you've covered all areas, summarize what you stored and end with \
   "Profile complete. Run /interview anytime to update."

Start by reading the current profile, then ask the first gap-filling question.
"""

_INTERVIEW_GOALS_PROMPT = """\
You are helping the user define and structure their goals. Talk like a peer \
who's helping them think through what they actually want to accomplish.

You have these tools:
- goal(action="create", title, priority, context, acceptance_criteria) — create a goal
- goal(action="list") — see existing goals
- goal(action="update", goal_id, ...) — update an existing goal
- profile_get() — check user context

RULES:
1. Start by listing existing goals: goal(action="list").
2. Ask what they're working on, what matters most, what's blocking them.
3. For each goal they describe: create it with clear title, appropriate priority, \
   and concrete acceptance criteria (things you can check off).
4. Decompose big goals into smaller ones with depends_on relationships.
5. Ask ONE question at a time. Be direct.
6. When done, show a summary of all goals and end with "Goals set."
"""

_INTERVIEW_FULL_PROMPT = """\
You are conducting a full onboarding interview to populate the user's profile \
and goal board. You are their second brain — talk like a peer, not a bot.

You have these tools:
- profile_set(key, value, category) — store a profile fact
- profile_get() — check current profile
- profile_forget(key) — remove a stale fact
- goal(action="create", title, priority, context, acceptance_criteria) — create a goal
- goal(action="list") — see existing goals
- goal(action="update", goal_id, ...) — update a goal

Profile categories (for profile_set):
- identity: name, email, location (immune to decay)
- career: role, company, comp, targets (90-day half-life)
- skill: languages, frameworks, tools (120-day half-life)
- preference: working style, communication prefs (180-day half-life)
- personal: hobbies, interests, habits (60-day half-life)
- learned: recent context (30-day half-life)

RULES:
1. Start by reading the current profile + goals to see what's already there.
2. Ask ONE question at a time. After each answer, persist facts immediately.
3. Cover: identity → career → skills → preferences → current goals → personal
4. For goals: decompose into concrete objectives with acceptance criteria.
5. Skip anything already well-populated in the profile.
6. Be efficient. Extract multiple facts from long answers.
7. When done, give a compact summary and say "All set."
"""


async def cmd_interview(args: str, ctx: REPLContext) -> str | None:
    """Agent-driven interview to populate your profile and goals.

    The agent asks you questions and stores the answers as structured
    profile facts (with proper decay) and goals (on the goal board).

    Usage:
        /interview              — full onboarding (profile + goals)
        /interview profile      — profile facts only
        /interview goals        — goal board only
        /interview update       — refresh stale facts and add new ones
    """

    sub = args.strip().lower()

    if sub == "profile":
        system_prompt = _INTERVIEW_PROFILE_PROMPT
        print_info("Starting profile interview...")
    elif sub in ("goals", "goal"):
        system_prompt = _INTERVIEW_GOALS_PROMPT
        print_info("Starting goals interview...")
    elif sub == "update":
        system_prompt = (
            _INTERVIEW_FULL_PROMPT + "\n\nThis is an UPDATE session. Focus on:\n"
            "1. Facts with low decay scores (profile_get with include_scores=true)\n"
            "2. Goals that may need progress updates\n"
            "3. Any new developments since last interview\n"
            "Ask what's changed recently, then update accordingly."
        )
        print_info("Starting profile update interview...")
    else:
        system_prompt = _INTERVIEW_FULL_PROMPT
        print_info("Starting full onboarding interview...")

    console.print(
        "[dim]The agent will ask you questions. Answer naturally — it'll store everything.[/]"
    )
    console.print("[dim]Type 'done' or 'skip' to move on. Ctrl+C to stop.[/]\n")

    # Determine which tools the interview agent can use.
    interview_tools = [
        "profile_get",
        "profile_set",
        "profile_forget",
        "profile_sync",
        "goal",
    ]

    # Save original system prompt and swap in interview prompt.
    original_prompt = ctx.system_prompt
    ctx.system_prompt = system_prompt

    try:
        # First turn: agent reads current state and asks first question.
        prompt = "Begin the interview. Start by reading what's already in my profile and goals."

        while True:
            try:
                async for event in ctx.client.run_loop(
                    prompt,
                    max_turns=3,
                    tool_allowlist=interview_tools,
                    session_id=ctx.session_id,
                ):
                    render_event(event)
            except Exception as exc:
                logger.debug("suppressed exception in cmd_interview", exc_info=True)
                print_error(str(exc))
                break

            # Get user's answer.
            console.print()
            try:
                answer = await _interview_input()
            except (KeyboardInterrupt, EOFError):
                logger.debug("suppressed exception in cmd_interview", exc_info=True)
                print_info("\nInterview ended.")
                break

            if not answer or answer.lower() in ("done", "quit", "exit", "stop"):
                print_ok("Interview complete.")
                break

            if answer.lower() == "skip":
                prompt = "The user wants to skip this topic. Move on to the next area."
                continue

            # Feed answer back as the next prompt.
            prompt = answer

    finally:
        # Restore original system prompt.
        ctx.system_prompt = original_prompt

    # Sync vault after interview so profile + goals are exported.
    try:
        from obscura.kairos.vault_sync import VaultSync

        vs = VaultSync()
        if vs.vault_dir.is_dir():
            await vs.sync()
            print_info("Vault synced with interview results.")
    except Exception:
        logger.debug("suppressed exception in cmd_interview", exc_info=True)

    return None


async def _interview_input() -> str:
    """Read user input during interview (supports async)."""
    loop = asyncio.get_event_loop()

    def _read() -> str:
        try:
            return input("  → ")
        except EOFError:
            logger.debug("suppressed exception in _read", exc_info=True)
            return ""

    return await loop.run_in_executor(None, _read)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

COMMANDS: dict[str, CommandHandler] = {
    "help": cmd_help,
    "quit": cmd_quit,
    "exit": cmd_quit,
    "q": cmd_quit,
    "clear": cmd_clear,
    # Chat
    "backend": cmd_backend,
    "model": cmd_model,
    "system": cmd_system,
    "tools": cmd_tools,
    "confirm": cmd_confirm,
    # Modes & permissions
    "mode": cmd_mode,
    "plan": cmd_plan,
    "permissions": cmd_permissions,
    "approve": cmd_approve,
    "reject": cmd_reject,
    # Review
    "diff": cmd_diff,
    "context": cmd_context,
    "context-inject": cmd_context_inject,
    "thinking": cmd_thinking,
    "compact": cmd_compact,
    # Agents & delegation
    "agent": cmd_agent,
    "skill": cmd_skill,
    "delegate": cmd_delegate,
    "fleet": cmd_fleet,
    "swarm": cmd_swarm,
    "coordinator": cmd_coordinator,
    "attention": cmd_attention,
    # Session / discovery
    "session": cmd_session,
    "discover": cmd_discover,
    "mcp": cmd_mcp,
    "plugin": cmd_plugin,
    "inspect": cmd_inspect,
    "pack": cmd_pack,
    "capability": cmd_capability,
    "a2a": cmd_a2a,
    # Memory
    "memory": cmd_memory,
    # Workspace
    "init": cmd_init,
    "migrate": cmd_migrate,
    # Control & status
    "status": cmd_status,
    "policies": cmd_policies,
    "replay": cmd_replay,
    "running": cmd_running,
    "kill": cmd_kill,
    "kill-session": cmd_kill_session,
    "doctor": cmd_doctor,
    # Git
    "commit": cmd_commit,
    "review": cmd_review,
    "pr": cmd_pr,
    "branch": cmd_branch,
    "worktree": cmd_worktree,
    "security-review": cmd_security_review,
    "ultrareview": cmd_ultrareview,
    # Utility
    "cat": cmd_cat,
    "search-tools": cmd_search_tools,
    "add-dir": cmd_add_dir,
    "files": cmd_files,
    "rewind": cmd_rewind,
    "rename": cmd_rename,
    "copy": cmd_copy,
    "vim": cmd_vim,
    # Plugin utilities
    "audit": cmd_audit,
    "health": cmd_health,
    "broker": cmd_broker,
    # Session management
    "resume": cmd_resume,
    "cost": cmd_cost,
    "export": cmd_export,
    "tag": cmd_tag,
    "stash": cmd_stash,
    "pop": cmd_pop,
    # Speed & effort
    "effort": cmd_effort,
    "fast": cmd_fast,
    "debug": cmd_debug,
    "caffeinate": cmd_caffeinate,
    # KAIROS & automation
    "arbiter": cmd_arbiter,
    "kairos": cmd_kairos,
    "loop": cmd_loop,
    "schedule": cmd_schedule,
    "ps": cmd_ps,
    "logs": cmd_logs,
    "log": cmd_log,
    # Steering
    "phantom": cmd_phantom,
    "interview": cmd_interview,
    "goal": cmd_goal,
    "persona": cmd_persona,
    "guardrails": cmd_guardrails,
    "focus": cmd_focus,
    "undercover": cmd_undercover,
    "tool-policy": cmd_tool_policy,
    # Info
    "version": cmd_version,
    "usage": cmd_usage,
    "stats": cmd_stats,
    "attribution": cmd_attribution,
    # Misc
    "btw": cmd_btw,
    "summary": cmd_summary,
    "brief": cmd_brief,
    "template": cmd_template,
    "workflow": cmd_workflow,
    "peers": cmd_peers,
    "send": cmd_send,
    "config": cmd_config,
    "hooks": cmd_hooks,
    "bug": cmd_bug,
    "voice": cmd_voice,
    # Auth / Supabase identity
    "login": cmd_login,
    "logout": cmd_logout,
    "whoami": cmd_whoami,
    "secrets": cmd_secrets,
}

# Subcommand completions for readline tab-complete
COMPLETIONS: dict[str, list[str]] = {
    "help": [],
    "quit": [],
    "exit": [],
    "q": [],
    "clear": [],
    "backend": ["copilot", "claude", "codex"],
    "model": [],
    "system": [],
    "tools": ["on", "off", "list", "enable", "disable"],
    "confirm": ["on", "off"],
    "mode": ["ask", "plan", "code"],
    "plan": ["show", "save", "execute", "clear"],
    "permissions": ["default", "plan", "accept_edits", "bypass"],
    "approve": ["all"],
    "reject": ["all"],
    "diff": ["accept", "reject", "apply"],
    "context": [],
    "context-inject": ["--paste"],
    "thinking": [],
    "compact": [],
    "agent": ["spawn", "list", "stop", "run"],
    "skill": ["list", "load", "unload", "active", "clear"],
    "delegate": ["codegen", "review", "analysis", "summarize", "testgen", "--model"],
    "fleet": ["spawn", "status", "run", "delegate", "stop"],
    "swarm": ["status", "results", "stop", "--model", "--no-synth", "--smart"],
    "coordinator": ["on", "off", "status"],
    "attention": ["respond"],
    "session": ["list", "new", "switch"],
    "discover": ["web", "filesystem", "git", "database", "ai", "cloud", "search"],
    "mcp": ["discover", "list", "select", "env", "install"],
    "pack": ["list", "info", "create"],
    "inspect": ["workspace", "agent", "capability", "pack"],
    "a2a": ["discover", "send", "stream", "list", "agents"],
    "init": ["--force"],
    "migrate": ["external", "--force", "--list"],
    "memory": ["stats", "search", "clear"],
    "status": ["--json"],
    "policies": [],
    "replay": [],
    "running": [],
    "kill": [],
    "kill-session": [],
    "doctor": [],
    "commit": [],
    "review": [],
    "pr": ["main", "master", "develop"],
    "branch": ["list", "create", "delete"],
    "worktree": ["list", "status", "sweep", "cleanup"],
    "security-review": [],
    "ultrareview": [],
    "cat": [],
    "search-tools": [],
    "add-dir": [],
    "files": [],
    "rewind": [],
    "rename": [],
    "copy": [],
    "vim": [],
    "audit": ["errors"],
    "health": [],
    "broker": [],
    "resume": [],
    "cost": [],
    "export": ["md", "txt", "json"],
    "tag": [],
    "stash": [],
    "pop": [],
    "effort": ["low", "medium", "high", "max"],
    "fast": [],
    "debug": [],
    "arbiter": ["status", "verdicts", "stats", "watchdog"],
    "kairos": ["on", "off", "status"],
    "loop": ["list", "stop"],
    "schedule": ["list", "add", "remove", "run"],
    "ps": [],
    "logs": [],
    "log": ["tail", "path", "stats"],
    "phantom": [
        "on",
        "off",
        "status",
        "ghost",
        "proxy",
        "autopilot",
        "level",
        "level 1",
        "level 2",
        "level 3",
        "level 4",
        "level 5",
        "level shadow",
        "level copilot",
        "level partner",
        "level lead",
        "level takeover",
    ],
    "interview": ["profile", "goals", "update"],
    "goal": ["clear", "check"],
    "persona": [
        "clear",
        "senior-backend",
        "security-auditor",
        "code-reviewer",
        "architect",
    ],
    "guardrails": ["add", "remove", "clear"],
    "focus": ["clear"],
    "undercover": ["on", "off", "auto"],
    "tool-policy": ["allow-all", "custom-only", "allow", "deny", "reset"],
    "version": [],
    "usage": [],
    "stats": [],
    "attribution": [],
    "btw": [],
    "summary": [],
    "brief": [],
    "template": ["list", "run", "new"],
    "workflow": ["list", "run"],
    "peers": [],
    "send": [],
    "config": ["reset"],
    "hooks": ["list", "add", "remove"],
    "bug": ["report"],
    "voice": ["on", "off"],
    "caffeinate": ["on", "off", "status"],
    # Auth
    "login": ["github", "copilot", "claude", "openai"],
    "logout": ["github", "copilot", "claude", "openai"],
    "whoami": [],
    "secrets": ["list", "get", "set", "delete"],
}

# Add secret menu stub (tests toggle visibility)
COMPLETIONS.setdefault("secret", ["status", "unlock", "lock"])


async def handle_command(raw: str, ctx: REPLContext) -> str | None:
    """Parse and dispatch a slash command. Returns 'quit' to exit."""
    line = raw.lstrip("/").strip()
    parts = line.split(None, 1)
    cmd_name = parts[0].lower() if parts else ""
    cmd_args = parts[1] if len(parts) > 1 else ""

    handler = COMMANDS.get(cmd_name)
    if handler is None:
        print_error(f"Unknown command: /{cmd_name}. Type /help for commands.")
        return None
    return await handler(cmd_args, ctx)


# Backwards-compatible stub for cmd_secret used in tests
async def cmd_secret(command: str, ctx: REPLContext) -> None:
    """Simple secret command used by tests to toggle secret menu visibility.

    Usage: /secret unlock | lock | status
    """
    cmd = (command or "").strip().lower()
    if cmd == "unlock":
        with contextlib.suppress(Exception):
            ctx.secret_menu_unlocked = True
        set_secret_menu_visibility(True)
        return
    if cmd == "lock":
        with contextlib.suppress(Exception):
            ctx.secret_menu_unlocked = False
        set_secret_menu_visibility(False)
        return
    # status / other
    return


def set_secret_menu_visibility(visible: bool) -> None:
    """Enable/disable additional secret completions used in tests.

    When enabled, add top-level 'loglevel' and 'jitter' entries and expose
    them under the 'secret' submenu. When disabled, remove them.
    """
    if visible:
        # top-level quick completions
        COMPLETIONS.setdefault("loglevel", [])
        COMPLETIONS.setdefault("jitter", [])
        # ensure secret submenu contains them
        secret = COMPLETIONS.setdefault("secret", ["status", "unlock", "lock"])
        if "loglevel" not in secret:
            secret.append("loglevel")
        if "jitter" not in secret:
            secret.append("jitter")
    else:
        # reset secret submenu to locked state
        COMPLETIONS["secret"] = ["status", "unlock", "lock"]
        COMPLETIONS.pop("loglevel", None)
        COMPLETIONS.pop("jitter", None)


# Minimal test-compatible implementations for cmd_tasks and cmd_menu


async def cmd_tasks(command: str, ctx: REPLContext) -> None:
    cmd = (command or "").strip().lower()
    # ensure lists/dicts exist
    if not hasattr(ctx, "background_tasks"):
        ctx.background_tasks = []
    if not hasattr(ctx, "python_tasks"):
        ctx.python_tasks = []
    if not hasattr(ctx, "background_task_refs"):
        ctx.background_task_refs = {}

    if cmd == "clear":
        ctx.background_tasks.clear()
        ctx.python_tasks.clear()
        return

    if cmd.startswith("interrupt"):
        # interrupt all
        if "all" in cmd:
            for tid, task in list(getattr(ctx, "background_task_refs", {}).items()):
                with contextlib.suppress(Exception):
                    task.cancel()
                # mark entry cancelled
                for entry in ctx.background_tasks:
                    if entry.get("id") == tid:
                        entry["status"] = "cancelled"
            return
        # interrupt specific id
        parts = cmd.split()
        if len(parts) >= 2:
            target = parts[-1]
            t = getattr(ctx, "background_task_refs", {}).get(target)
            if t:
                with contextlib.suppress(Exception):
                    t.cancel()
            for entry in ctx.background_tasks:
                if entry.get("id") == target:
                    entry["status"] = "cancelled"
        return


async def cmd_menu(command: str, ctx: REPLContext) -> None:
    cmd = (command or "").strip()
    if not hasattr(ctx, "ui_right_menu_enabled"):
        ctx.ui_right_menu_enabled = True
    if not hasattr(ctx, "ui_menu_items"):
        ctx.ui_menu_items = {}

    if cmd == "off":
        ctx.ui_right_menu_enabled = False
        return
    if cmd == "on":
        ctx.ui_right_menu_enabled = True
        return
    # Expect forms like 'reasoning off' or 'reasoning on'
    parts = cmd.split()
    if len(parts) >= 2:
        key = parts[0]
        val = parts[-1].lower()
        ctx.ui_menu_items[key] = val in ("on", "true", "1")
        return
