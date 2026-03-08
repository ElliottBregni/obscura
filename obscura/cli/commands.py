"""obscura.cli.commands — Slash command registry and handlers."""

from __future__ import annotations

import json
import os
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from rich.table import Table

from obscura.cli.render import (
    console,
    print_error,
    print_info,
    print_ok,
    print_warning,
    render_agent_output,
    render_attention_request,
    render_diff_summary,
    render_plan,
)
from obscura.core.context_window import estimate_tokens as _cw_estimate_tokens
from obscura.core.client import ObscuraClient
from obscura.cli import trace as trace_mod
from obscura.cli.control_commands import cmd_heartbeat, cmd_policies, cmd_replay, cmd_status
from obscura.core.context_lazy import LazySkillLoader, SkillMetadata
from obscura.core.event_store import SQLiteEventStore, SessionStatus
from obscura.core.paths import resolve_obscura_skills_dir
from obscura.core.types import AgentEventKind, Backend, SessionRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MESSAGE_ROLE_OVERHEAD_TOKENS = 4
_RESPONSE_RESERVE_TOKENS = 4096


def _estimate_tokens(text: str) -> int:
    """Estimate token count using shared context-window tokenizer."""
    return _cw_estimate_tokens(text)


def _safe_list_tools(ctx: "REPLContext") -> list[Any]:
    """Best-effort tool list retrieval from the active client."""
    try:
        tools = ctx.client.list_tools()
    except Exception:
        return []
    if not isinstance(tools, list):
        return []
    return tools


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
            }
        )
    return _estimate_tokens(json.dumps(payload, default=str, ensure_ascii=True))


def _estimate_claude_tool_listing_tokens(tools: list[Any]) -> int:
    """Estimate Claude's extra tool-listing text appended to system prompt."""
    if not tools:
        return 0

    lines = ["## Available Tools", ""]
    lines.append("You have the following tools. Use these EXACT names when calling tools:")
    lines.append("")
    for spec in tools:
        desc = str(getattr(spec, "description", "") or "").split("\n")[0][:120]
        lines.append(f"- `{getattr(spec, 'name', '')}`: {desc}")
    lines.append("")
    lines.append("Do NOT invent tool names. If none of these tools fit, tell the user.")
    return _estimate_tokens("\n".join(lines))


def estimate_effective_context_breakdown(
    ctx: "REPLContext",
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
        _estimate_claude_tool_listing_tokens(tools)
        if ctx.backend == "claude"
        else 0
    )

    response_reserve_tokens = _RESPONSE_RESERVE_TOKENS if include_response_reserve else 0

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
    ctx: "REPLContext",
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
    }
)


# ---------------------------------------------------------------------------
# REPL context — mutable state shared across the session
# ---------------------------------------------------------------------------


@dataclass
class REPLContext:
    """Mutable state for the REPL session."""

    client: ObscuraClient
    store: SQLiteEventStore
    session_id: str
    backend: str
    model: str | None
    system_prompt: str
    max_turns: int
    tools_enabled: bool
    mcp_configs: list[dict[str, Any]] = field(default_factory=list)

    # Approval gates
    confirm_enabled: bool = False
    confirm_always: set[str] = field(default_factory=set)

    # Message history for context tracking
    message_history: list[tuple[str, str]] = field(default_factory=list)

    # File change tracking for /diff (path -> {path, original, modified})
    _file_changes: list[dict[str, str]] = field(default_factory=list, repr=False)
    _pending_file_reads: dict[str, tuple[str, str]] = field(
        default_factory=dict, repr=False
    )

    # Mode manager (lazy)
    _mode_manager: Any = field(default=None, repr=False)

    # Vector memory store (None if disabled)
    vector_store: Any = field(default=None, repr=False)

    # Agent runtime (lazy-created on first /agent or /fleet command)
    _runtime: Any = field(default=None, repr=False)

    # Background swarm tasks: {swarm_id: {task, assignments, results, ...}}
    _swarm_runs: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    # Slash-skill state (metadata lazy-loaded, bodies loaded on activation)
    _lazy_skill_loader: LazySkillLoader | None = field(default=None, repr=False)
    active_skills: list[str] = field(default_factory=list)

    def get_mode_manager(self) -> Any:
        """Get or create the ModeManager."""
        if self._mode_manager is None:
            from obscura.cli.app.modes import ModeManager, TUIMode

            self._mode_manager = ModeManager(TUIMode.CODE)
        return self._mode_manager

    def get_effective_system_prompt(self) -> str:
        """Combine mode system prompt with user system prompt."""
        mode_prompt = ""
        if self._mode_manager is not None:
            mode_prompt = self._mode_manager.get_system_prompt()
        if mode_prompt and self.system_prompt:
            return f"{mode_prompt}\n\n{self.system_prompt}"
        return mode_prompt or self.system_prompt

    async def get_runtime(self) -> Any:
        """Get or create the AgentRuntime, wiring InteractionBus to CLI."""
        if self._runtime is None:
            from obscura.agent.agents import AgentRuntime
            from obscura.agent.interaction import (
                AgentOutput,
                AttentionRequest,
            )
            from obscura.auth.models import AuthenticatedUser

            user = AuthenticatedUser(
                user_id=os.environ.get("USER", "local"),
                email="cli@obscura.local",
                roles=("operator",),
                org_id="local",
                token_type="user",
                raw_token="",
            )
            self._runtime = AgentRuntime(user)
            await self._runtime.start()

            # Wire InteractionBus → CLI
            bus = self._runtime.interaction_bus

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
                """Render agent output streamed via the bus."""
                render_agent_output(output)

            bus.on_attention(_cli_attention_handler)
            bus.on_output(_cli_output_handler)

        return self._runtime

    async def stop_runtime(self) -> None:
        """Stop the runtime if it was created."""
        if self._runtime is not None:
            await self._runtime.stop()
            self._runtime = None

    async def recreate_client(self, backend: str, model: str | None) -> None:
        """Stop old client, create a new one for a different backend."""
        await self.client.stop()
        new_client = ObscuraClient(
            backend,
            model=model,
            system_prompt=self.get_effective_system_prompt(),
            mcp_servers=self.mcp_configs or None,
        )
        await new_client.start()
        if self.tools_enabled:
            from obscura.tools.system import get_system_tool_specs

            for spec in get_system_tool_specs():
                new_client.register_tool(spec)
        self.client = new_client
        self.backend = backend
        self.model = model

    def add_file_change(self, path: str, original: str, modified: str) -> None:
        """Track a file change for /diff. Dedupes by path."""
        self._file_changes = [
            c for c in self._file_changes if c["path"] != path
        ]
        self._file_changes.append(
            {"path": path, "original": original, "modified": modified}
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
        "[bold]Commands:[/]",
        "  [cyan]/help[/]                Show this message",
        "  [cyan]/quit[/]                Exit",
        "  [cyan]/clear[/]               Clear screen",
        "",
        " [bold]Chat[/]",
        "  [cyan]/backend[/] [name]      Show or switch backend (copilot, claude, codex)",
        "  [cyan]/model[/] [name]        Show or switch model",
        "  [cyan]/system[/] <prompt>     Set system prompt",
        "  [cyan]/tools[/] [on|off|list] Show, toggle, or list tools",
        "  [cyan]/confirm[/] [on|off]    Tool approval gates",
        "",
        " [bold]Modes[/]",
        "  [cyan]/mode[/] [ask|plan|code] Switch interaction mode",
        "  [cyan]/approve[/] <n|all>     Approve plan step(s)",
        "  [cyan]/reject[/] <n|all>      Reject plan step(s)",
        "  [cyan]/plan[/]                Show current plan",
        "",
        " [bold]Review[/]",
        "  [cyan]/diff[/] [accept|reject|apply] Review file changes",
        "  [cyan]/context[/]             Show context window stats",
        "  [cyan]/compact[/] [n]         Compact context (keep last n messages)",
        "",
        " [bold]Agents[/]",
        "  [cyan]/agent[/] [cmd]         spawn | list | stop | run",
        "  [cyan]/skill[/] [cmd]         list | load | unload | active | clear",
        "  [cyan]/fleet[/] [cmd]         spawn | status | run | delegate | stop",
        "  [cyan]/attention[/] [cmd]     List or respond to agent attention requests",
        "  [cyan]/tail-trace[/] [n]    Tail recent trace entries",
        "",
        " [bold]Session[/]",
        "  [cyan]/session[/] [cmd]       list | new | <id>",
        "  [cyan]/discover[/] [cat] [n]  Discover popular MCP tools",
        "",
        " [bold]Workspace[/]",
        "  [cyan]/init[/] [--force]      Init local .obscura/ workspace",
        "",
        " [bold]Control[/]",
        "  [cyan]/heartbeat[/]           Session health check (no LLM)",
        "  [cyan]/status[/]              Alias for /heartbeat",
        "  [cyan]/tools[/] [on|off|list] Show, toggle, or list tools",
        "  [cyan]/policies[/]            List policy versions",
        "  [cyan]/replay[/] <run_id>     Replay supervisor run events",
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


async def cmd_tail_trace(args: str, _ctx: REPLContext) -> str | None:
    """Tail recent JSONL trace entries (from logs/trace.log). Usage: /tail-trace [n]"""
    try:
        n = int(args.strip()) if args.strip() else 50
    except Exception:
        n = 50
    try:
        from obscura.cli.trace import tail_pretty

        out = tail_pretty(n)
        if not out:
            print_info("No trace entries found.")
        else:
            console.print(out)
    except Exception:
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
            f"Unknown backend: {name}. Use 'copilot', 'claude', or 'codex'."
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
    """Show or toggle tool calling."""
    val = args.strip().lower()
    if not val:
        print_info(f"Tools: {'on' if ctx.tools_enabled else 'off'}")
        return None
    if val == "on":
        ctx.tools_enabled = True
        print_ok("Tools enabled.")
    elif val == "off":
        ctx.tools_enabled = False
        print_ok("Tools disabled.")
    elif val == "list":
        try:
            tools = ctx.client.list_tools()
            if not tools:
                print_info("No tools registered.")
                return None
            from obscura.cli.render import TOOL_COLOR
            table = Table(title="Registered Tools", expand=False)
            table.add_column("#", justify="right", style="dim", width=4)
            table.add_column("name", style=TOOL_COLOR, no_wrap=True)
            table.add_column("description", max_width=60)
            for i, t in enumerate(tools, 1):
                desc = getattr(t, "description", "") or ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                table.add_row(str(i), t.name, desc)
            console.print(table)
        except Exception as exc:
            print_error(f"Failed to list tools: {exc}")
    else:
        print_error("Usage: /tools [on|off|list]")
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
    from obscura.cli.app.modes import TUIMode

    mm = ctx.get_mode_manager()
    val = args.strip().lower()

    if not val:
        print_info(f"Mode: {mm.current.value}")
        return None

    mode_map = {"ask": TUIMode.ASK, "plan": TUIMode.PLAN, "code": TUIMode.CODE}
    mode = mode_map.get(val)
    if mode is None:
        print_error("Usage: /mode ask|plan|code")
        return None

    mm.switch(mode)

    # ASK / PLAN modes disable tools; CODE enables them
    if mode in (TUIMode.ASK, TUIMode.PLAN):
        ctx.tools_enabled = False
    elif mode == TUIMode.CODE:
        ctx.tools_enabled = True

    # Recreate client with mode-specific system prompt
    await ctx.recreate_client(ctx.backend, ctx.model)
    print_ok(f"Switched to {val} mode.")
    return None


async def cmd_plan(_args: str, ctx: REPLContext) -> str | None:
    """Show the current plan."""
    mm = ctx.get_mode_manager()
    plan = mm.active_plan
    if plan is None:
        print_info("No active plan. Use /mode plan and ask the model to create one.")
        return None
    render_plan(plan)
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
        render_diff_summary(ctx._file_changes)
        return None

    if sub == "accept":
        return await _diff_accept_reject(rest, ctx, accept=True)
    if sub == "reject":
        return await _diff_accept_reject(rest, ctx, accept=False)
    if sub == "apply":
        return await _diff_apply(ctx)

    print_error("Usage: /diff [accept|reject|apply] [n|all]")
    return None


async def _diff_accept_reject(
    val: str, ctx: REPLContext, *, accept: bool
) -> str | None:
    """Accept or reject hunks."""
    from obscura.cli.app.diff_engine import DiffEngine

    if not ctx._file_changes:
        print_info("No file changes.")
        return None

    engine = DiffEngine()
    # Build flat hunk list
    all_hunks = []
    for fc in ctx._file_changes:
        diff_fc = engine.compute_change(
            Path(fc["path"]), fc["original"], fc["modified"]
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


async def _diff_apply(ctx: REPLContext) -> str | None:
    """Apply accepted hunks to disk."""
    from obscura.cli.app.diff_engine import DiffEngine

    if not ctx._file_changes:
        print_info("No file changes.")
        return None

    engine = DiffEngine()
    applied = 0
    for fc in ctx._file_changes:
        diff_fc = engine.compute_change(
            Path(fc["path"]), fc["original"], fc["modified"]
        )
        accepted = [h for h in diff_fc.hunks if h.status == "accepted"]
        if not accepted:
            continue
        patched = engine.apply_hunks(fc["original"], accepted)
        Path(fc["path"]).write_text(patched)
        applied += 1
        print_ok(f"  Applied {len(accepted)} hunks to {fc['path']}")

    if applied:
        ctx._file_changes.clear()
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
        f"tools={breakdown['tool_schema_tokens']:,}"
    )
    if breakdown["claude_tool_listing_tokens"]:
        console.print(
            f"  claude_tool_listing={breakdown['claude_tool_listing_tokens']:,}"
        )
    console.print(f"  response_reserve={breakdown['response_reserve_tokens']:,}")
    mm = ctx.get_mode_manager()
    console.print(f"Mode: {mm.current.value}")
    if tokens > 80_000:
        console.print("[yellow]Warning: context is large. Consider /compact[/]")
    return None


async def cmd_thinking(_args: str, _ctx: REPLContext) -> str | None:
    """Show expanded thinking/reasoning blocks from the last response."""
    from obscura.cli.render import _active_renderer, console, THINKING_COLOR
    from rich.panel import Panel
    from rich.text import Text

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
            )
        )
    console.print(f"[dim]{len(blocks)} thinking block(s)[/]")
    return None


async def cmd_compact(args: str, ctx: REPLContext) -> str | None:
    """Compact context by starting a fresh session with summary."""
    keep = 4
    val = args.strip()
    if val and val.isdigit():
        keep = int(val)

    if len(ctx.message_history) <= keep:
        print_info("Not enough history to compact.")
        return None

    old = ctx.message_history[:-keep]
    before = estimate_effective_context_tokens(ctx)

    # Build brief summary of dropped messages
    summary_lines: list[str] = []
    for role, text in old:
        snippet = text[:200].replace("\n", " ")
        summary_lines.append(f"[{role}]: {snippet}")
    summary = "\n".join(summary_lines)
    if len(summary) > 2000:
        summary = summary[:2000] + "..."

    # Fresh session with summary prepended
    dropped = len(old)
    ctx.message_history = ctx.message_history[-keep:]
    ctx.session_id = uuid.uuid4().hex
    ctx.system_prompt = (
        f"[Previous conversation summary ({dropped} messages)]\n{summary}\n\n"
        + ctx.system_prompt
    )
    await ctx.recreate_client(ctx.backend, ctx.model)

    after = estimate_effective_context_tokens(ctx)
    print_ok(
        f"Compacted: dropped {dropped} messages, "
        f"~{max(0, before - after):,} effective tokens freed. New session: {ctx.session_id[:12]}"
    )
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
        tbl.add_column("Status", style="yellow")
        tbl.add_column("Backend", style="dim")
        tbl.add_column("Model", style="dim")
        tbl.add_column("Agent", style="green")
        tbl.add_column("Msgs", style="dim", justify="right")
        tbl.add_column("Created", style="dim")
        for s in rows[:20]:
            indicator = "[bold cyan]\u2192[/]" if s.id == current_id else ""
            tbl.add_row(
                indicator,
                s.id[:12],
                s.status.value,
                s.backend or "-",
                s.model or "-",
                s.active_agent or "-",
                str(s.message_count) if s.message_count else "-",
                s.created_at.strftime("%Y-%m-%d %H:%M"),
            )
        return tbl

    if active:
        console.print(_build_table(active, f"Active ({len(active)})"))
    if other:
        style = "bold dim" if active else "bold"
        console.print(
            _build_table(other[:10], f"Completed ({len(other)})", style)
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
        label = (
            f"{s.id[:8]} · {s.status.value}"
            f" · {s.active_agent or s.backend or 'default'}"
        )
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
        pass


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
            f"{len(matches)} sessions. Be more specific."
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
        try:
            await ctx.recreate_client(ctx.backend, ctx.model)
            print_ok(f"Switched to session: {session_id[:12]} (reconnected)")
        except Exception as exc:
            # Can't recover — revert
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
    """Spawn agent from manifest. Usage: /agent spawn <name> [-m model] [-s system_prompt]"""
    tokens = shlex.split(args) if args else []
    if not tokens:
        print_error("Usage: /agent spawn <name> [-m model] [-s system_prompt]")
        return None

    name = tokens[0]
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

    # SECURITY FIX: Load manifest from ~/.obscura/agents.yaml
    import yaml
    from obscura.core.paths import resolve_obscura_home
    from obscura.manifest.models import AgentManifest

    agents_yaml = resolve_obscura_home() / "agents.yaml"
    manifest_loaded = False

    if agents_yaml.exists():
        try:
            with open(agents_yaml) as f:
                config = yaml.safe_load(f)
                agent_configs = {a["name"]: a for a in config.get("agents", [])}

                if name in agent_configs:
                    cfg = agent_configs[name]

                    # Daemon agents are auto-started, not manually spawned
                    if cfg.get("type") == "daemon":
                        print_warning(
                            f"'{name}' is a daemon agent (auto-started at session start). "
                            "Use /agent list to see running daemons."
                        )
                        return None

                    # Build AgentManifest from YAML config
                    # Extract skills config dict if present
                    skills_cfg = cfg.get("skills", {})
                    if not isinstance(skills_cfg, dict):
                        skills_cfg = {}

                    manifest = AgentManifest(
                        name=cfg["name"],
                        provider=model_override
                        or cfg.get("provider")
                        or cfg.get("model", ctx.backend),
                        system_prompt=system_prompt_override or cfg.get("system_prompt", ""),
                        max_turns=cfg.get("max_turns", 10),
                        tools=cfg.get("tools", []),
                        tags=cfg.get("tags", []),
                        mcp_servers=cfg.get("mcp_servers", []) if isinstance(cfg.get("mcp_servers"), list) else [],
                        skills_config=skills_cfg,
                    )

                    # Spawn from manifest (SECURE) — pass explicit model override if given
                    agent = runtime.spawn_from_manifest(
                        manifest,
                        provider_override=model_override,
                    )
                    await agent.start()
                    print_ok(
                        f"✓ Spawned {name} from manifest (id: {agent.id[:12]}, "
                        f"max_turns: {cfg.get('max_turns', 10)})"
                    )
                    manifest_loaded = True
                    return None

        except Exception as e:
            print_warning(f"Failed to load manifest for '{name}': {e}")

    # Fallback: spawn with SDK defaults (with warning)
    if not manifest_loaded:
        print_warning(
            f"⚠ No manifest found for '{name}' in {agents_yaml}. "
            "Using SDK defaults (no skill filters, tool restrictions, or limits)."
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
    """Run a prompt on an agent. Usage: /agent run <id|name> <prompt>"""
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

    from obscura.cli.render import render_event

    try:
        async for event in agent.stream_loop(prompt):
            render_event(event)
        console.print()
    except Exception as exc:
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
            "[--done-if TEXT] <task_type|--model MODEL> <prompt>"
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
                print_error("--max-turns must be a positive integer.")
                return None
            i += 2
        elif tokens[i] == "--passes" and i + 1 < len(tokens):
            try:
                max_passes = int(tokens[i + 1])
                if max_passes <= 0:
                    raise ValueError
            except ValueError:
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
        agent_name, model=model,
        system_prompt="You are a specialized " + (task_type or model) + " subagent. Complete the task concisely.",
    )
    await agent.start()
    print_info("=> Delegating to [" + model + "] in " + mode + " mode...")
    from obscura.cli.render import render_event
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
                        + " passes."
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
        print_error("Delegation failed: " + str(exc))
        return None
    finally:
        try:
            await agent.stop()
        except Exception:
            pass
    if collected_output and ctx.client and hasattr(ctx.client, "inject_context"):
        summary = "\n".join(collected_output)
        ctx.client.inject_context("[Delegated to " + model + "]\n" + summary)
        print_ok("Injected " + str(len(summary)) + " chars into context")
    return None

# ---------------------------------------------------------------------------
# Handlers — interaction bus (attention requests)
# ---------------------------------------------------------------------------


async def cmd_attention(args: str, ctx: REPLContext) -> str | None:
    """List or respond to pending attention requests from agents."""
    parts = args.strip().split(None, 2)
    sub = parts[0] if parts else ""

    if sub == "respond" or sub == "r":
        # /attention respond <request_id_prefix> <action> [text]
        if len(parts) < 3:
            print_error("Usage: /attention respond <id> <action> [text]")
            return None
        rid_prefix = parts[1]
        rest = parts[2].split(None, 1)
        action = rest[0]
        text = rest[1] if len(rest) > 1 else ""

        if ctx._runtime is None:
            print_error("No runtime active.")
            return None
        bus = ctx._runtime.interaction_bus
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
    if ctx._runtime is None:
        print_info("No runtime active. Use /fleet spawn first.")
        return None

    bus = ctx._runtime.interaction_bus
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
    """Spawn multiple agents. Usage: /fleet spawn <name1> [name2...] [-m model]"""
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
    for name in names:
        # SECURITY FIX: Try to load from manifest first
        from pathlib import Path as P
        import yaml
        from obscura.manifest.models import AgentManifest

        agents_yaml = P.home() / ".obscura" / "agents.yaml"
        manifest_loaded = False

        if agents_yaml.exists():
            try:
                with open(agents_yaml) as f:
                    config = yaml.safe_load(f)
                    agent_configs = {a["name"]: a for a in config.get("agents", [])}

                    if name in agent_configs and agent_configs[name].get("enabled", True):
                        cfg = agent_configs[name]
                        s_cfg = cfg.get("skills", {})
                        if not isinstance(s_cfg, dict):
                            s_cfg = {}
                        manifest = AgentManifest(
                            name=cfg["name"],
                            provider=model or cfg.get("provider") or cfg.get("model", ctx.backend),
                            system_prompt=cfg.get("system_prompt", ""),
                            max_turns=cfg.get("max_turns", 10),
                            tools=cfg.get("tools", []),
                            tags=cfg.get("tags", []),
                            mcp_servers=cfg.get("mcp_servers", []) if isinstance(cfg.get("mcp_servers"), list) else [],
                            skills_config=s_cfg,
                        )
                        agent = runtime.spawn_from_manifest(
                            manifest,
                            provider_override=model if model != ctx.backend else None,
                        )
                        manifest_loaded = True
            except Exception:
                pass

        if not manifest_loaded:
            agent = runtime.spawn(name, model=model)
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

    from obscura.cli.render import LabeledStreamRenderer

    # Color rotation for agents
    colors = ["cyan", "magenta", "yellow", "green", "blue", "red"]

    for idx, agent in enumerate(running):
        color = colors[idx % len(colors)]
        renderer = LabeledStreamRenderer(agent.config.name, color)
        try:
            async for event in agent.stream_loop(prompt):
                renderer.handle(event)
        except KeyboardInterrupt:
            renderer.finish()
            console.print("[dim][interrupted][/]")
            break
        except Exception as exc:
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
            "[--max-turns N] [--passes N] [--done-if TEXT] <prompt>"
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
                print_error("--max-turns must be a positive integer.")
                return None
            i += 2
        elif tokens[i] == "--passes" and i + 1 < len(tokens):
            try:
                max_passes = int(tokens[i + 1])
                if max_passes <= 0:
                    raise ValueError
            except ValueError:
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
            "[--max-turns N] [--passes N] [--done-if TEXT] <prompt>"
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

    from obscura.cli.render import LabeledStreamRenderer

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
                        + " passes."
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
        renderer.finish()
        console.print("[dim][interrupted][/]")
    except Exception as exc:
        renderer.finish()
        print_error(str(exc))
    else:
        renderer.finish()
    console.print()
    return None


async def _fleet_stop(args: str, ctx: REPLContext) -> str | None:
    """Stop fleet agents. Usage: /fleet stop [name|all]"""
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
        matches = [a for a in agents if a.config.name == target or a.id.startswith(target)]
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
    (["code", "implement", "write", "function", "class", "module", "refactor", "feature"],
     "code-architect", "Code implementation task"),
    (["python", "pip", "pytest", "type hint", "pep", "dataclass", "pydantic"],
     "python-dev", "Python-specific task"),
    (["test", "unit test", "coverage", "assert", "mock", "fixture"],
     "python-dev", "Testing task"),
    (["bug", "fix", "error", "crash", "traceback", "debug", "breakpoint"],
     "debugger", "Debugging task"),
    (["review", "pr", "pull request", "code review", "lint"],
     "github-pr-reviewer", "Code review task"),
    (["security", "vuln", "cve", "auth", "xss", "injection", "pentest"],
     "security-researcher", "Security analysis task"),
    (["deploy", "docker", "k8s", "ci", "cd", "pipeline", "infra", "terraform"],
     "devops-engineer", "Infrastructure/DevOps task"),
    (["research", "analyze", "investigate", "compare", "benchmark"],
     "research-analyst", "Research/analysis task"),
    (["doc", "readme", "api doc", "changelog", "tutorial"],
     "technical-writer", "Documentation task"),
    (["design", "ux", "ui", "wireframe", "mockup", "layout"],
     "ux-designer", "Design task"),
    (["data", "ml", "model", "dataset", "train", "predict", "pandas"],
     "data-scientist", "Data science task"),
    (["prompt", "system prompt", "instruct"],
     "prompt-engineer", "Prompt engineering task"),
    (["product", "prd", "roadmap", "prioritize", "stakeholder"],
     "product-manager", "Product management task"),
    (["content", "blog", "copy", "seo", "marketing"],
     "content-writer", "Content creation task"),
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
    from obscura.tools.swarm import build_agent_catalog

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

    items = json.loads(raw)
    if not isinstance(items, list):
        raise ValueError("Expected JSON array from planner")

    assignments: list[_SwarmAssignment] = []
    for item in items:
        name = item.get("agent_name", "assistant")
        if name not in agent_configs:
            if "assistant" in agent_configs:
                name = "assistant"
        assignments.append(_SwarmAssignment(
            agent_name=name,
            prompt=item.get("prompt", task),
            rationale=item.get("rationale", ""),
        ))
    return assignments


async def _swarm_run_agent(
    assignment: _SwarmAssignment,
    runtime: Any,
    agent_configs: dict[str, dict[str, Any]],
    model_override: str | None,
    ctx: REPLContext,
) -> tuple[str, str]:
    """Spawn, loop, and stop a single swarm agent. Returns (name, output)."""
    from obscura.manifest.models import AgentManifest

    name = assignment.agent_name
    agent = None

    try:
        cfg = agent_configs.get(name)
        if cfg is not None:
            s_cfg = cfg.get("skills", {})
            if not isinstance(s_cfg, dict):
                s_cfg = {}
            manifest = AgentManifest(
                name=cfg["name"],
                provider=model_override or cfg.get("provider") or cfg.get("model", ctx.backend),
                system_prompt=cfg.get("system_prompt", ""),
                max_turns=cfg.get("max_turns", 25),
                tools=cfg.get("tools", []),
                tags=cfg.get("tags", []),
                mcp_servers=(
                    cfg.get("mcp_servers", [])
                    if isinstance(cfg.get("mcp_servers"), list)
                    else []
                ),
                skills_config=s_cfg,
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
        error_msg = f"Error: {exc}"
        print_error(f"  {name}: {error_msg}")
        return (name, error_msg)

    finally:
        if agent is not None:
            try:
                await agent.stop()
            except Exception:
                pass


async def _swarm_synthesize(
    task: str,
    results: list[tuple[str, str]],
    ctx: REPLContext,
) -> str:
    """Synthesize agent results using the session LLM."""
    agent_results = "\n\n".join(
        f"### {name}\n{output}" for name, output in results
    )
    prompt = _SWARM_SYNTH_PROMPT.format(task=task, agent_results=agent_results)
    try:
        message = await ctx.client.send(prompt)
        return message.text
    except Exception:
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
    import asyncio as _aio

    run = ctx._swarm_runs[swarm_id]
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
        results: list[tuple[str, str]] = await _aio.gather(*coros)
        run["results"] = results

        # Synthesize
        if synthesize and len(results) > 1:
            summary = await _swarm_synthesize(task, results, ctx)
        else:
            summary = "\n\n".join(
                f"## {name}\n{output}" for name, output in results
            )

        run["summary"] = summary
        run["status"] = "done"

        # Inject into context
        if summary and ctx.client and hasattr(ctx.client, "inject_context"):
            ctx.client.inject_context(
                f"[Swarm results for: {task[:80]}]\n{summary}"
            )

        # Notify user
        print_ok(f"Swarm [{swarm_id}] complete — /swarm status to see results")

    except Exception as exc:
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
    import asyncio as _aio

    from obscura.tools.swarm import load_agent_configs

    tokens = shlex.split(args) if args else []
    if not tokens:
        print_info(
            "Usage:\n"
            "  /swarm <task>            Launch a swarm\n"
            "  /swarm status            Show swarm runs\n"
            "  /swarm results [id]      Show results\n"
            "  /swarm stop [id|all]     Cancel swarm(s)\n"
            "\n"
            "Flags: --model MODEL  --no-synth  --smart"
        )
        return None

    sub = tokens[0]

    # --- /swarm status ---
    if sub == "status":
        if not ctx._swarm_runs:
            print_info("No swarm runs.")
            return None
        table = Table(show_header=True, header_style="bold", title="Swarm Runs")
        table.add_column("ID", style="cyan", width=10)
        table.add_column("Task", style="dim", max_width=40)
        table.add_column("Agents", style="yellow", width=8)
        table.add_column("Status", style="green")
        for sid, run in ctx._swarm_runs.items():
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
        from rich.markdown import Markdown
        from rich.rule import Rule

        target = tokens[1] if len(tokens) > 1 else None
        runs = ctx._swarm_runs
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
        for sid, run in list(ctx._swarm_runs.items()):
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
    ctx._swarm_runs[swarm_id] = run_state

    bg_task = _aio.create_task(
        _swarm_background(
            swarm_id=swarm_id,
            task=task,
            assignments=assignments,
            runtime=runtime,
            agent_configs=agent_configs,
            model_override=model_override,
            synthesize=synthesize,
            ctx=ctx,
        )
    )
    run_state["_task"] = bg_task

    print_ok(
        f"Swarm [{swarm_id}] launched with {len(assignments)} agents — "
        "prompt is free. Use /swarm status or /swarm results to check."
    )
    return None


# ---------------------------------------------------------------------------
# Handlers — MCP discovery
# ---------------------------------------------------------------------------


async def cmd_discover(args: str, ctx: REPLContext) -> str | None:
    """Discover popular MCP tools dynamically."""
    from obscura.tools.dynamic_discovery import DynamicToolDiscovery

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
            "[dim]Categories: web, filesystem, git, database, ai, cloud, search[/]\n"
        )
    except Exception as exc:
        print_error(f"Discovery failed: {exc}")

    return None


# ---------------------------------------------------------------------------
# MCP Management Commands
# ---------------------------------------------------------------------------


async def cmd_mcp(args: str, ctx: REPLContext) -> str | None:
    """MCP server management commands."""
    from obscura.cli.mcp_commands import handle_mcp_command

    try:
        args_list = shlex.split(args) if args.strip() else []
    except ValueError:
        args_list = args.split()

    handle_mcp_command(args_list)
    return None


async def cmd_plugin(args: str, ctx: REPLContext) -> str | None:
    """Manage Obscura plugins.

    Usage:
      /plugin list
      /plugin install <package-or-path-or-git>
      /plugin remove <name>
    """
    try:
        from obscura.tools.plugin_registry import PluginRegistry
    except Exception:
        print_error("Plugin management not available. Ensure obscura.tools.plugin_registry is present.")
        return None

    registry = PluginRegistry()

    try:
        tokens = shlex.split(args) if args and args.strip() else []
    except ValueError:
        tokens = args.split()

    if not tokens:
        print_info("Usage: /plugin [list|install|remove]")
        return None

    sub = tokens[0]
    if sub == "list":
        data = registry.list_installed()
        local = data.get("local", []) if isinstance(data, dict) else data
        regs = data.get("registered", []) if isinstance(data, dict) else []
        if not local and not regs:
            print_info("No plugins installed.")
            return None
        if local:
            print_info("Local plugins:")
            for p in local:
                console.print(f"  • [cyan]{p.get('name')}[/] — {p.get('path')}")
        if regs:
            print_info("Registered plugins (pip/git):")
            for r in regs:
                console.print(f"  • [cyan]{r.get('name')}[/] — {r.get('type')} {r.get('source', r.get('package', ''))}")
        return None

    if sub == "install":
        if len(tokens) < 2:
            print_error("Usage: /plugin install <package-or-path-or-git>")
            return None
        source = tokens[1]
        print_info(f"Installing plugin: {source}")
        res = registry.install(source)
        if res.get("ok"):
            print_ok(res.get("message"))
        else:
            print_error(res.get("message"))
        return None

    if sub in ("remove", "uninstall"):
        if len(tokens) < 2:
            print_error("Usage: /plugin remove <name>")
            return None
        name = tokens[1]
        print_info(f"Removing plugin: {name}")
        res = registry.remove(name)
        if res.get("ok"):
            print_ok(res.get("message"))
        else:
            print_error(res.get("message"))
        return None

    print_info("Unknown subcommand. Usage: /plugin [list|install|remove]")
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
    """Discover remote A2A agent. Usage: /a2a discover <url>"""
    if not url:
        print_error("Usage: /a2a discover <url>")
        return None

    try:
        from obscura.integrations.a2a.client import A2AClient
    except ImportError:
        print_error("A2A integration not available. Install with: pip install obscura[a2a]")
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
            table.add_row("Push Notifications", "✓" if card.capabilities.pushNotifications else "✗")

            console.print(table)

            if card.skills:
                print_info(f"\n{len(card.skills)} Skills:")
                for skill in card.skills:
                    console.print(f"  • [bold]{skill.name}[/bold]: {skill.description}")

    except Exception as exc:
        print_error(f"Discovery failed: {exc}")
    return None


async def _a2a_send(args: str, ctx: REPLContext) -> str | None:
    """Send message to A2A agent. Usage: /a2a send <url> <message>"""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_error("Usage: /a2a send <url> <message>")
        return None

    url, message = parts

    try:
        from obscura.integrations.a2a.client import A2AClient
    except ImportError:
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
                        console.print(f"\n[bold green]Agent Response:[/bold green]")
                        for part in msg.parts:
                            if hasattr(part, 'text'):
                                console.print(part.text)

    except Exception as exc:
        print_error(f"Send failed: {exc}")
    return None


async def _a2a_stream(args: str, ctx: REPLContext) -> str | None:
    """Stream message to A2A agent. Usage: /a2a stream <url> <message>"""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_error("Usage: /a2a stream <url> <message>")
        return None

    url, message = parts

    try:
        from obscura.integrations.a2a.client import A2AClient
    except ImportError:
        print_error("A2A integration not available")
        return None

    try:
        async with A2AClient(url) as client:
            print_info(f"Streaming message to {url}...")
            async for event in client.stream_message(message):
                if event.kind == "status-update":
                    console.print(f"[dim]Status: {event.status.state.value}[/dim]")
                elif event.kind == "artifact-update":
                    console.print(f"[green]Artifact: {event.artifact.name or 'unnamed'}[/green]")

    except Exception as exc:
        print_error(f"Stream failed: {exc}")
    return None


async def _a2a_list_tasks(url: str, ctx: REPLContext) -> str | None:
    """List tasks on remote agent. Usage: /a2a list <url>"""
    if not url:
        print_error("Usage: /a2a list <url>")
        return None

    try:
        from obscura.integrations.a2a.client import A2AClient
    except ImportError:
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
                    str(len(task.history))
                )

            console.print(table)
            if next_cursor:
                print_info(f"More tasks available (cursor: {next_cursor[:12]}...)")

    except Exception as exc:
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
            "Vector memory is disabled. Set OBSCURA_VECTOR_MEMORY=on to enable."
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
            print_error(f"Could not get stats: {exc}")

    elif subcmd == "search":
        if not rest:
            print_warning("Usage: /memory search <query>")
            return None
        try:
            results = ctx.vector_store.search_reranked(
                rest, top_k=5, recency_weight=0.2
            )
            if not results:
                print_info("No results found.")
            else:
                for i, r in enumerate(results, 1):
                    text_preview = r.text[:150].replace("\n", " ")
                    console.print(
                        f"  [bold]{i}.[/] (score: {r.score:.2f}) {text_preview}"
                    )
        except Exception as exc:
            print_error(f"Search failed: {exc}")

    elif subcmd == "clear":
        try:
            from obscura.cli.vector_memory_bridge import (
                CLI_NAMESPACE,
                clear_mcp_noise_memories,
            )

            scope = rest.strip().lower()
            if scope in {"mcp", "mcp-logs", "mcp_logs"}:
                count = clear_mcp_noise_memories(ctx.vector_store)
                print_ok(
                    f"Cleared {count} MCP-related auto-saved memories from CLI namespace."
                )
            else:
                count = ctx.vector_store.clear_namespace(CLI_NAMESPACE)
                print_ok(f"Cleared {count} auto-saved memories from CLI namespace.")
        except Exception as exc:
            print_error(f"Clear failed: {exc}")

    else:
        print_info("Usage: /memory [stats|search <query>|clear [mcp]]")

    return None


# ---------------------------------------------------------------------------
# Workspace init
# ---------------------------------------------------------------------------


async def cmd_init(args: str, _ctx: REPLContext) -> str | None:
    """Initialise a local .obscura/ workspace in the current directory."""
    from obscura.core.workspace import WorkspaceExistsError, init_workspace

    force = "--force" in args
    try:
        ws = init_workspace(force=force)
        print_ok(f"Workspace initialised at {ws}")
    except WorkspaceExistsError:
        print_warning(
            ".obscura/ already exists. Use /init --force to reinitialise."
        )
    except Exception as exc:
        print_error(f"Init failed: {exc}")
    return None


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
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    state = agent.get_state()
    parts: list[object] = []

    # ── Header: agent state snapshot ──
    s_c = "green" if state.status.name == "RUNNING" else "yellow"
    parts.append(Text.from_markup(
        f"[bold {s_c}]{state.status.name}[/]"
        f"  ·  iters: {state.iteration_count}"
        f"  ·  id: {agent.id[:12]}",
    ))
    if state.error_message:
        parts.append(Text.from_markup(
            f"[bold red]Error:[/] {state.error_message}",
        ))

    # ── Current thinking delta / active text ──
    try:
        from obscura.cli.render import get_active_text

        active_text = get_active_text()
        if active_text:
            preview = active_text[-500:]
            if len(active_text) > 500:
                preview = "…" + preview
            parts.append(Text(""))
            parts.append(Text.from_markup(
                "[bold]Thinking delta:[/]",
            ))
            parts.append(Text(preview, style="dim italic"))
    except Exception:
        pass

    # ── Recent trace events ──
    try:
        from obscura.cli.trace import tail_entries

        entries = tail_entries(30)
        if entries:
            parts.append(Text(""))
            parts.append(Text.from_markup(
                "[bold]Recent activity:[/]",
            ))
            shown = 0
            for e in reversed(entries):
                if shown >= 15:
                    break
                kind = e.get("kind", "?")
                preview = e.get("preview", "")
                tools = e.get("tool_names", [])
                ts_raw = e.get("ts", "")
                ts_short = (
                    ts_raw[11:19] if len(ts_raw) > 19 else ts_raw
                )
                tool_tag = ""
                if tools:
                    tool_tag = (
                        f" [cyan]({', '.join(tools)})[/]"
                    )
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                line = (
                    f"  [dim]{ts_short}[/]"
                    f" [{_event_color(kind)}]{kind}[/]"
                    f"{tool_tag}"
                )
                if preview:
                    line += f" [dim]{preview}[/]"
                parts.append(Text.from_markup(line))
                shown += 1
    except Exception:
        pass

    # ── Recent session events from event store ──
    try:
        events = await ctx.store.get_events(ctx.session_id)
        if events:
            keep = {
                "tool_call", "tool_result", "error",
                "turn_start", "turn_complete",
                "context_compact",
            }
            interesting = [
                ev for ev in events if ev.kind.value in keep
            ]
            tail = interesting[-10:]
            if tail:
                parts.append(Text(""))
                parts.append(Text.from_markup(
                    "[bold]Session events:[/]",
                ))
                for ev in tail:
                    ts = ev.timestamp.strftime("%H:%M:%S")
                    payload = ev.payload
                    detail = ""
                    if ev.kind.value == "tool_call":
                        detail = (
                            "→ " + payload.get("tool_name", "?")
                        )
                    elif ev.kind.value == "tool_result":
                        detail = str(
                            payload.get("result", ""),
                        )[:60]
                    elif ev.kind.value == "error":
                        detail = str(
                            payload.get("message", ""),
                        )[:60]
                    c = _event_color(ev.kind.value)
                    line = (
                        f"  [dim]{ts}[/]"
                        f" [{c}]{ev.kind.value}[/]"
                    )
                    if detail:
                        line += f" [dim]{detail}[/]"
                    parts.append(Text.from_markup(line))
    except Exception:
        pass

    if not parts:
        parts.append(Text("  No data available.", style="dim"))

    panel = Panel(
        Group(*parts),
        title=f"Agent: {agent.config.name}",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print(panel)


async def cmd_running(args: str, ctx: REPLContext) -> str | None:
    """Show running agents, daemons, and session activity."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    from obscura.agent.agents import AgentStatus

    target = args.strip()
    lines: list[object] = []
    has_activity = False
    selectable: list[Any] = []

    # ------------------------------------------------------------------
    # 1. Current session
    # ------------------------------------------------------------------
    sid_short = (
        ctx.session_id[:8] if ctx.session_id else "none"
    )
    session_rec = (
        await ctx.store.get_session(ctx.session_id)
        if ctx.session_id
        else None
    )
    status_val = (
        session_rec.status.value if session_rec else "active"
    )
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
    if ctx._runtime is not None:
        try:
            agents_list = ctx._runtime.list_agents()
        except Exception:
            pass

    active_set = {
        AgentStatus.RUNNING,
        AgentStatus.WAITING,
        AgentStatus.PENDING,
    }

    if agents_list:
        active = [
            a for a in agents_list if a.status in active_set
        ]
        terminal = [
            a for a in agents_list
            if a.status not in active_set
        ]
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
                "Iters", style="dim", justify="right",
            )
            tbl.add_column("ID", style="cyan")
            for i, a in enumerate(active, 1):
                state = a.get_state()
                sc = (
                    "bold green"
                    if a.status == AgentStatus.RUNNING
                    else "yellow"
                )
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
                "Iters", style="dim", justify="right",
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
    # 3. Other running sessions
    # ------------------------------------------------------------------
    try:
        all_sessions = await ctx.store.list_sessions()
        s_active = {
            SessionStatus.RUNNING,
            SessionStatus.WAITING_FOR_TOOL,
        }
        other = [
            s for s in all_sessions
            if s.id != ctx.session_id
            and s.status in s_active
        ]
        if other:
            has_activity = True
            stbl = Table(
                show_header=True,
                header_style="bold",
                title=(
                    f"Other Sessions ({len(other)} running)"
                ),
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
        pass

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
            a for a in selectable
            if a.config.name == target
            or a.id.startswith(target)
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
        choices = [
            a.config.name for a in selectable
        ] + ["back"]
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
                match = [
                    a for a in selectable
                    if a.config.name == result.action
                ]
                if match:
                    await _running_detail(match[0], ctx)
        except Exception:
            pass

    return None


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
    # Modes
    "mode": cmd_mode,
    "plan": cmd_plan,
    "approve": cmd_approve,
    "reject": cmd_reject,
    # Review
    "diff": cmd_diff,
    "context": cmd_context,
    "thinking": cmd_thinking,
    "compact": cmd_compact,
    # Agents
    "agent": cmd_agent,
    "skill": cmd_skill,
    "delegate": cmd_delegate,
    "fleet": cmd_fleet,
    "swarm": cmd_swarm,
    "attention": cmd_attention,
    "tail-trace": cmd_tail_trace,
    # Session / discovery
    "session": cmd_session,
    "discover": cmd_discover,
    "mcp": cmd_mcp,
    "plugin": cmd_plugin,
    "a2a": cmd_a2a,
    # Memory
    "memory": cmd_memory,
    # Workspace
    "init": cmd_init,
    # Control
    "heartbeat": cmd_heartbeat,
    "hb": cmd_heartbeat,
    "status": cmd_status,
    "policies": cmd_policies,
    "replay": cmd_replay,
    "running": cmd_running,
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
    "tools": ["on", "off", "list"],
    "confirm": ["on", "off"],
    "mode": ["ask", "plan", "code"],
    "plan": [],
    "approve": ["all"],
    "reject": ["all"],
    "diff": ["accept", "reject", "apply"],
    "context": [],
    "thinking": [],
    "compact": [],
    "agent": ["spawn", "list", "stop", "run"],
    "skill": ["list", "load", "unload", "active", "clear"],
    "delegate": ["codegen", "review", "analysis", "summarize", "testgen", "support", "--model"],
    "fleet": ["spawn", "status", "run", "delegate", "stop"],
    "swarm": ["status", "results", "stop", "--model", "--no-synth", "--smart"],
    "attention": ["respond"],
    "session": ["list", "new", "switch"],
    "discover": ["web", "filesystem", "git", "database", "ai", "cloud", "search"],
    "mcp": ["discover", "list", "select", "env", "install"],
    "a2a": ["discover", "send", "stream", "list", "agents"],
    "tail-trace": [],
    "init": ["--force"],
    "memory": ["stats", "search", "clear"],
    "heartbeat": ["--json"],
    "hb": ["--json"],
    "status": ["--json"],
    "policies": [],
    "replay": [],
    "running": [],
}


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
