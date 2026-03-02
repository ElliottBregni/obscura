"""obscura.cli.commands — Slash command registry and handlers."""

from __future__ import annotations

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
from obscura.core.client import ObscuraClient
from obscura.cli import trace as trace_mod
from obscura.cli.control_commands import cmd_heartbeat, cmd_policies, cmd_replay, cmd_status
from obscura.core.event_store import SQLiteEventStore, SessionStatus
from obscura.core.types import AgentEventKind, Backend, SessionRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token)."""
    return len(text) // 4


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
                """Prompt user inline when an agent requests attention."""
                render_attention_request(request)
                actions = request.actions
                prompt_str = (
                    f"  [{'/'.join(actions)}]: "
                    if actions and actions != ("ok",)
                    else "  [ok]: "
                )
                from obscura.cli.prompt import confirm_prompt_async

                answer = await confirm_prompt_async(prompt_str)
                if not answer:
                    answer = actions[0] if actions else "ok"
                action = answer if answer in actions else actions[0]
                text = answer if answer not in actions else ""
                await bus.respond(request.request_id, action, text)

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
        "  [cyan]/fleet[/] [cmd]         spawn | status | run | delegate | stop",
        "  [cyan]/attention[/] [cmd]     List or respond to agent attention requests",
        "  [cyan]/tail-trace[/] [n]    Tail recent trace entries",
        "",
        " [bold]Session[/]",
        "  [cyan]/session[/] [cmd]       list | new | <id>",
        "  [cyan]/discover[/] [cat] [n]  Discover popular MCP tools",
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
    if not ctx.message_history:
        print_info("No messages yet.")
        return None
    total_text = "".join(t for _, t in ctx.message_history)
    tokens = _estimate_tokens(total_text)
    user_msgs = sum(1 for r, _ in ctx.message_history if r == "user")
    asst_msgs = sum(1 for r, _ in ctx.message_history if r == "assistant")
    console.print(f"Messages: {user_msgs} user, {asst_msgs} assistant")
    console.print(f"Estimated tokens: {tokens:,}")
    mm = ctx.get_mode_manager()
    console.print(f"Mode: {mm.current.value}")
    if tokens > 80_000:
        console.print("[yellow]Warning: context is large. Consider /compact[/]")
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
    before = _estimate_tokens("".join(t for _, t in ctx.message_history))

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

    after = _estimate_tokens("".join(t for _, t in ctx.message_history))
    print_ok(
        f"Compacted: dropped {dropped} messages, "
        f"~{before - after:,} tokens freed. New session: {ctx.session_id[:12]}"
    )
    return None


# ---------------------------------------------------------------------------
# Handlers — session
# ---------------------------------------------------------------------------


async def cmd_session(args: str, ctx: REPLContext) -> str | None:
    """Session management: list, new, or resume by ID."""
    sub = args.strip()

    if sub == "list" or not sub:
        sessions = await ctx.store.list_sessions()
        if not sessions:
            print_info("No sessions.")
            return None
        table = Table(show_header=True, header_style="bold")
        table.add_column("Session", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Agent", style="green")
        table.add_column("Created", style="dim")
        for s in sessions[:20]:
            table.add_row(
                s.id[:12],
                s.status.value,
                s.active_agent,
                s.created_at.strftime("%Y-%m-%d %H:%M"),
            )
        console.print(table)
        return None

    if sub == "new":
        new_id = uuid.uuid4().hex
        ctx.session_id = new_id
        print_ok(f"New session: {new_id}")
        return None

    # Resume by ID
    existing = await ctx.store.get_session(sub)
    if existing is None:
        print_error(f"Session {sub} not found.")
        return None
    ctx.session_id = sub
    try:
        await ctx.client.resume_session(
            SessionRef(session_id=sub, backend=Backend(ctx.backend))
        )
    except Exception:
        pass  # best-effort resume
    print_ok(f"Resumed session: {sub}")
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
    from pathlib import Path
    import yaml
    from obscura.manifest.models import AgentManifest

    agents_yaml = Path.home() / ".obscura" / "agents.yaml"
    manifest_loaded = False

    if agents_yaml.exists():
        try:
            with open(agents_yaml) as f:
                config = yaml.safe_load(f)
                agent_configs = {a["name"]: a for a in config.get("agents", [])}

                if name in agent_configs:
                    cfg = agent_configs[name]

                    # Build AgentManifest from YAML config
                    # Extract skills config dict if present
                    skills_cfg = cfg.get("skills", {})
                    if not isinstance(skills_cfg, dict):
                        skills_cfg = {}

                    manifest = AgentManifest(
                        name=cfg["name"],
                        model=model_override or cfg.get("model", ctx.backend),
                        system_prompt=system_prompt_override or cfg.get("system_prompt", ""),
                        max_turns=cfg.get("max_turns", 10),
                        tools=cfg.get("tools", []),
                        tags=cfg.get("tags", []),
                        mcp_servers=cfg.get("mcp_servers", "auto"),
                        skills_config=skills_cfg,
                    )

                    # Spawn from manifest (SECURE)
                    agent = runtime.spawn_from_manifest(manifest)
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
                    
                    if name in agent_configs:
                        cfg = agent_configs[name]
                        s_cfg = cfg.get("skills", {})
                        if not isinstance(s_cfg, dict):
                            s_cfg = {}
                        manifest = AgentManifest(
                            name=cfg["name"],
                            model=model or cfg.get("model", ctx.backend),
                            system_prompt=cfg.get("system_prompt", ""),
                            max_turns=cfg.get("max_turns", 10),
                            tools=cfg.get("tools", []),
                            tags=cfg.get("tags", []),
                            mcp_servers=cfg.get("mcp_servers", "auto"),
                            skills_config=s_cfg,
                        )
                        agent = runtime.spawn_from_manifest(manifest)
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
    """Send prompt to specific agent. Usage: /fleet delegate <agent> <prompt>"""
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        print_error("Usage: /fleet delegate <agent> <prompt>")
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

    from obscura.cli.render import LabeledStreamRenderer

    renderer = LabeledStreamRenderer(agent.config.name, "cyan")
    try:
        async for event in agent.stream_loop(prompt):
            renderer.handle(event)
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
            from obscura.cli.vector_memory_bridge import CLI_NAMESPACE

            count = ctx.vector_store.clear_namespace(CLI_NAMESPACE)
            print_ok(f"Cleared {count} auto-saved memories from CLI namespace.")
        except Exception as exc:
            print_error(f"Clear failed: {exc}")

    else:
        print_info("Usage: /memory [stats|search <query>|clear]")

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
    "compact": cmd_compact,
    # Agents
    "agent": cmd_agent,
    "fleet": cmd_fleet,
    "attention": cmd_attention,
    "tail-trace": cmd_tail_trace,
    # Session / discovery
    "session": cmd_session,
    "discover": cmd_discover,
    "mcp": cmd_mcp,
    "a2a": cmd_a2a,
    # Memory
    "memory": cmd_memory,
    # Control
    "heartbeat": cmd_heartbeat,
    "hb": cmd_heartbeat,
    "status": cmd_status,
    "policies": cmd_policies,
    "replay": cmd_replay,
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
    "compact": [],
    "agent": ["spawn", "list", "stop", "run"],
    "fleet": ["spawn", "status", "run", "delegate", "stop"],
    "attention": ["respond"],
    "session": ["list", "new"],
    "discover": ["web", "filesystem", "git", "database", "ai", "cloud", "search"],
    "mcp": ["discover", "list", "select", "env", "install"],
    "a2a": ["discover", "send", "stream", "list", "agents"],
    "tail-trace": [],
    "memory": ["stats", "search", "clear"],
    "heartbeat": ["--json"],
    "hb": ["--json"],
    "status": ["--json"],
    "policies": [],
    "replay": [],
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
