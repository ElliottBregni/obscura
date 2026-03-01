"""obscura.cli.render — Event rendering for the interactive REPL."""

from __future__ import annotations

from typing import Any
import os

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from datetime import datetime

from obscura.core.types import AgentEvent, AgentEventKind

import re


def _sanitize_text(s: str) -> str:
    """Remove ANSI escape/control sequences from text for safe printing."""
    if not s:
        return ""
    try:
        # Remove CSI sequences (ESC [ ... ) and other common ANSI sequences
        cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", s)
        # Remove other control characters (except newline and tab)
        cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+", "", cleaned)
        return cleaned
    except Exception:
        return s


class OutputManager:
    """Lightweight output manager for routing internal debug and prints.

    This manager can buffer outputs for non-CLI envs and optionally surface
    internal prints when verbose_internals is True.
    """

    def __init__(self, env: str = "cli", verbose_internals: bool = False) -> None:
        self.env = env
        self.verbose = verbose_internals
        self._buffer: list[str] = []

    def capture_internal(self, message: str) -> None:
        if not self.verbose:
            return
        # Always capture to internal buffer for diagnostics
        try:
            self._buffer.append(message)
        except Exception:
            pass

        # Sanitize common control/ANSI sequences so internals don't pollute the prompt
        try:
            import re

            cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", message)
        except Exception:
            cleaned = message

        if self.env == "cli":
            # Use the real console to print internals with dim style when in CLI
            try:
                # prefer parsing ANSI and printing safely
                Console().print(Text.from_ansi(cleaned))
            except Exception:
                try:
                    Console().print(f"[dim][internal][/]{cleaned}")
                except Exception:
                    pass
        else:
            # keep sanitized version in non-cli buffers
            try:
                # replace buffer entry with cleaned version for diagnostics
                if self._buffer:
                    self._buffer[-1] = cleaned
            except Exception:
                pass

    def get_buffer(self) -> list[str]:
        return list(self._buffer)


from obscura import config

# global output manager (can be used by other modules to send internals)
output = OutputManager(env=config.OUTPUT_MODE, verbose_internals=config.VERBOSE)

# Optionally capture builtins.print into the output manager when requested
if config.CAPTURE_PRINTS:
    import builtins

    _orig_print = builtins.print

    def _capturing_print(*args, **kwargs):
        # Always call the original print
        _orig_print(*args, **kwargs)
        try:
            output.capture_internal(" ".join(str(a) for a in args))
        except Exception:
            pass

    builtins.print = _capturing_print

# existing console for rich rendering
console = Console(force_terminal=False, no_color=True, legacy_windows=False)

# Active renderer for expand-preview hotkey (set by send_message)
_active_renderer: "StreamRenderer" | None = None

def set_active_renderer(r: "StreamRenderer" | None) -> None:
    """Register the currently active StreamRenderer (for preview expansion)."""
    global _active_renderer
    _active_renderer = r

def get_active_text() -> str:
    """Return the currently accumulated text from the active renderer, if any."""
    try:
        if _active_renderer is None:
            return ""
        return _active_renderer.get_accumulated_text()
    except Exception:
        return ""



# ---------------------------------------------------------------------------
# StreamRenderer — stateful Markdown renderer with tool/thinking display
# ---------------------------------------------------------------------------


class StreamRenderer:
    """Accumulates text deltas and renders as Markdown on flush.

    Handles thinking indicators, tool-call spinners, and error display.
    If an external rich.status.Status is provided, update it inline instead of printing
    below the prompt (used for background streaming placeholders).
    """

    def __init__(self, external_status: object | None = None) -> None:
        self._text_buf: list[str] = []
        self._thinking_buf: list[str] = []
        self._all_text: list[str] = []
        self._in_thinking = False
        self._status: object | None = None  # internal spinner/status
        self._external_status: object | None = external_status

    def handle(self, event: AgentEvent) -> None:
        """Process a single AgentEvent."""
        match event.kind:
            case AgentEventKind.THINKING_DELTA:
                if not self._in_thinking:
                    self._flush_text()
                    self._in_thinking = True
                self._thinking_buf.append(event.text)

            case AgentEventKind.TEXT_DELTA:
                if self._in_thinking:
                    self._flush_thinking()
                self._text_buf.append(event.text)
                # Keep a running copy of all assistant text for preview expansion
                try:
                    self._all_text.append(event.text)
                except Exception:
                    pass

            case AgentEventKind.TOOL_CALL:
                self._flush_all()
                self._show_tool_call(event)

            case AgentEventKind.TOOL_RESULT:
                self._stop_spinner()
                self._show_tool_result(event)

            case AgentEventKind.TURN_COMPLETE | AgentEventKind.AGENT_DONE:
                self._flush_all()

            case AgentEventKind.ERROR:
                self._flush_all()
                print_error(event.text)

            case _:
                pass

    # -- flush helpers -------------------------------------------------------

    def _flush_text(self) -> None:
        if self._text_buf:
            text = "".join(self._text_buf)
            self._text_buf.clear()
            if text.strip():
                # Add a small blank line for visual buffer before assistant output
                console.print()
                # Always print the content to the chat (sanitize ANSI/control chars)
                safe_text = _sanitize_text(text)
                console.print(Markdown(safe_text))
                # If using external status, keep it active with a concise timestamped preview
                if self._external_status is not None:
                    try:
                        ts = datetime.now().strftime("%H:%M:%S")
                        preview = safe_text.strip().replace('\n', ' ')[:120]
                        self._external_status.update(f"› assistant [{ts}]: {preview}")  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _flush_thinking(self) -> None:
        if self._thinking_buf:
            text = "".join(self._thinking_buf)
            self._thinking_buf.clear()
            self._in_thinking = False
            if text.strip():
                if self._external_status is not None:
                    try:
                        ts = datetime.now().strftime("%H:%M:%S")
                        preview = ("[thinking] " + text.strip()).replace('\n', ' ')[:120]
                        self._external_status.update(f"› thinking [{ts}]: {preview}")  # type: ignore[attr-defined]
                    except Exception:
                        # Fallback: add spacing and print a thinking panel
                        console.print()
                        console.print(
                            Panel(
                                Text(text.strip(), style="dim italic"),
                                title="[dim]thinking[/]",
                                border_style="dim",
                                expand=False,
                            )
                        )
                else:
                    # Add spacing for readability before thinking panel
                    console.print()
                    console.print(
                        Panel(
                            Text(text.strip(), style="dim italic"),
                            title="[dim]thinking[/]",
                            border_style="dim",
                            expand=False,
                        )
                    )

    def _flush_all(self) -> None:
        self._flush_thinking()
        self._flush_text()
        self._stop_spinner()

    # -- tool display --------------------------------------------------------

    def _show_tool_call(self, event: AgentEvent) -> None:
        name = event.tool_name
        parts: list[str] = []
        for k, v in event.tool_input.items():
            sv = str(v)
            if len(sv) > 60:
                sv = sv[:57] + "..."
            parts.append(f"{k}={sv}")
        arg_str = ", ".join(parts)
        if len(arg_str) > 120:
            arg_str = arg_str[:117] + "..."

        sanitized_args = _sanitize_text(arg_str)
        label = f"  [bold cyan]⚡ {name}[/]"
        if sanitized_args:
            label += f"  [dim]{sanitized_args}[/]"
        
        # Capture as internal output as well (sanitized)
        try:
            output.capture_internal(f"TOOL_CALL {name} {sanitized_args}")
        except Exception:
            pass

        # ALWAYS print to console so user can see the tool activity
        console.print(label)

        # Also update external status if present
        if self._external_status is not None:
            try:
                ts = datetime.now().strftime("%H:%M:%S")
                self._external_status.update(f"› tool [{ts}]: {name}")  # type: ignore[attr-defined]
            except Exception:
                pass

        try:
            if self._external_status is None:
                self._status = console.status(
                    "  [dim]running...[/]", spinner="dots"
                )
                self._status.start()  # type: ignore[union-attr]
        except Exception:
            self._status = None

    def _show_tool_result(self, event: AgentEvent) -> None:
        raw_snippet = (event.tool_result or "")[:200]
        snippet = _sanitize_text(raw_snippet)
        style = "red" if event.is_error else "dim"
        prefix = "✗" if event.is_error else "→"
        out = f"  [{style}]{prefix} {snippet}[/]"
        
        # ALWAYS print to console so user can see the tool results
        console.print(out)
        
        # Also update external status if present
        if self._external_status is not None:
            try:
                ts = datetime.now().strftime("%H:%M:%S")
                self._external_status.update(f"› assistant [{ts}]: {snippet}")  # type: ignore[attr-defined]
            except Exception:
                pass

    def _stop_spinner(self) -> None:
        if self._status is not None:
            try:
                self._status.stop()  # type: ignore[union-attr]
            except Exception:
                pass
            self._status = None
        # If using external status, clear it to avoid stale content
        if self._external_status is not None:
            try:
                self._external_status.update("")  # type: ignore[attr-defined]
            except Exception:
                pass

    def finish(self) -> None:
        """Call at end of message to flush remaining state."""
        self._flush_all()

    def get_accumulated_text(self) -> str:
        """Return the full accumulated assistant text collected so far."""
        try:
            parts: list[str] = []
            parts.extend(self._all_text)
            # include any buffered text not yet appended
            parts.extend(self._text_buf)
            parts.extend(self._thinking_buf)
            return "".join(parts)
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# LabeledStreamRenderer — for fleet multi-agent output
# ---------------------------------------------------------------------------


class LabeledStreamRenderer(StreamRenderer):
    """StreamRenderer variant that prefixes output with an agent label."""

    def __init__(self, label: str, color: str = "cyan") -> None:
        super().__init__()
        self._label = label
        self._color = color
        self._header_printed = False

    def _flush_text(self) -> None:
        if self._text_buf:
            text = "".join(self._text_buf)
            self._text_buf.clear()
            if text.strip():
                if not self._header_printed:
                    console.print(
                        f"\n[bold {self._color}]── {self._label} ──[/]"
                    )
                    self._header_printed = True
                console.print(Markdown(text))


# ---------------------------------------------------------------------------
# Legacy render_event — kept for /agent run streaming
# ---------------------------------------------------------------------------


def render_event(event: AgentEvent) -> None:
    """Simple single-event renderer (no Markdown accumulation)."""
    match event.kind:
        case AgentEventKind.TEXT_DELTA:
            console.print(_sanitize_text(event.text), end="")
        case AgentEventKind.TOOL_CALL:
            console.print(f"\n  [dim]⚡ {_sanitize_text(event.tool_name)}[/]")
        case AgentEventKind.TOOL_RESULT:
            snippet = _sanitize_text((event.tool_result or "")[:120])
            console.print(f"  [dim]→ {snippet}[/]")
        case _:
            pass


# ---------------------------------------------------------------------------
# Plan / Diff rendering
# ---------------------------------------------------------------------------


def render_plan(plan: Any) -> None:
    """Render a Plan with step statuses."""
    console.print(f"\n[bold]{plan.title}[/]\n")
    for step in plan.steps:
        if step.status == "approved":
            icon = "[green]✓[/]"
        elif step.status == "rejected":
            icon = "[red]✗[/]"
        elif step.status == "edited":
            icon = "[yellow]✎[/]"
        else:
            icon = "[dim]○[/]"
        console.print(f"  {icon} [cyan]{step.number}.[/] {step.description}")
    console.print(
        f"\n[dim]{plan.approved_count} approved · "
        f"{plan.rejected_count} rejected · "
        f"{plan.pending_count} pending[/]"
    )
    if plan.all_decided:
        console.print("[green]All steps decided. /mode code to execute.[/]")
    else:
        console.print("[dim]/approve <n|all> · /reject <n|all>[/]")


def render_diff_summary(changes: list[Any]) -> None:
    """Render a summary of file changes with hunk details."""
    if not changes:
        print_info("No file changes in this session.")
        return
    from obscura.tui.diff_engine import DiffEngine

    engine = DiffEngine()
    hunk_idx = 0
    for fc in changes:
        diff_fc = engine.compute_change(fc["path"], fc["original"], fc["modified"])
        console.print(f"\n[bold]{fc['path']}[/]  ({len(diff_fc.hunks)} hunks)")
        unified = engine.format_unified(diff_fc)
        if unified.strip():
            try:
                from rich.syntax import Syntax

                console.print(Syntax(unified, "diff", theme="monokai"))
            except Exception:
                console.print(unified)
        for hunk in diff_fc.hunks:
            status_icon = {"accepted": "✓", "rejected": "✗"}.get(
                hunk.status, "○"
            )
            console.print(
                f"  [dim]hunk {hunk_idx}: "
                f"@@ -{hunk.old_start},{hunk.old_count} "
                f"+{hunk.new_start},{hunk.new_count} @@ "
                f"[{status_icon}][/]"
            )
            hunk_idx += 1
    console.print(f"\n[dim]/diff accept <n|all> · /diff reject <n|all> · /diff apply[/]")


# ---------------------------------------------------------------------------
# InteractionBus rendering
# ---------------------------------------------------------------------------


def render_attention_request(request: Any) -> None:
    """Render an AttentionRequest from the InteractionBus."""
    priority_style = {
        "low": "dim",
        "normal": "yellow",
        "high": "bold yellow",
        "critical": "bold red",
    }
    pri = getattr(request.priority, "value", "normal")
    style = priority_style.get(pri, "yellow")

    console.print(
        Panel(
            Text(request.message),
            title=f"[{style}]⚠ {request.agent_name}[/]",
            subtitle=f"[dim]{request.request_id[:12]}[/]",
            border_style=style,
            expand=False,
        )
    )
    actions = request.actions
    if actions and actions != ("ok",):
        console.print(f"  [dim]Actions: {', '.join(actions)}[/]")


def render_agent_output(output: Any) -> None:
    """Render an AgentOutput from the InteractionBus."""
    if not output.text:
        return
    safe_text = _sanitize_text(output.text)
    if output.is_final:
        console.print(f"  [bold cyan]{_sanitize_text(output.agent_name)}:[/] {safe_text}")
    else:
        console.print(safe_text, end="")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def print_banner(
    backend: str,
    model: str | None,
    session_id: str,
    *,
    tool_count: int = 0,
    mcp_servers: list[str] | None = None,
    mode: str = "code",
) -> None:
    """Print the REPL startup banner."""
    label = backend
    if model:
        label += f", {model}"

    # Compose a compact header similar to Codex style
    info_parts: list[str] = []
    if tool_count:
        info_parts.append(f"{tool_count} tools")
    if mcp_servers:
        info_parts.append(f"MCP: {', '.join(mcp_servers)}")
    info_parts.append(f"mode: {mode}")
    info_line = "  ".join(info_parts)

    header = Text()
    header.append(" >_ ", style="bold")
    header.append(f"Obscura ({label})\n")
    header.append("\n")
    header.append(f"model:     {model or 'default'}   /model to change\n")
    header.append(f"directory: ~\n")

    console.print(Panel(header, expand=False, border_style="magenta"))
    if info_line:
        console.print(f"[dim]{info_line}[/]")
    console.print("[dim]Type /help for commands, /quit to exit.[/]\n")


def print_error(msg: str) -> None:
    """Print an error message."""
    console.print(f"[bold red]Error:[/] {msg}")


def print_info(msg: str) -> None:
    """Print an info message."""
    console.print(f"[dim]{msg}[/]")


def print_warning(msg: str) -> None:
    """Print a warning message."""
    console.print(f"[yellow]{msg}[/]")


def print_ok(msg: str) -> None:
    """Print a success message."""
    console.print(f"[bold green]{msg}[/]")


# ---------------------------------------------------------------------------
# Markdown transcript export helper
# ---------------------------------------------------------------------------

def export_transcript_markdown(history: list[tuple[str, str]]) -> str:
    """Export a conversation history to a Markdown-formatted transcript.

    `history` is a list of tuples (role, text) where role is 'user' or 'assistant'.
    Returns a single Markdown string suitable for saving or display.
    """
    lines: list[str] = []
    lines.append("# Conversation transcript\n")
    for idx, (role, text) in enumerate(history, start=1):
        if role == "user":
            lines.append(f"### {idx}. User\n")
            lines.append("```\n" + text.strip() + "\n```")
            lines.append("")
        else:
            # Assistant output should be treated as Markdown; include verbatim but with a bullet prefix.
            lines.append(f"> Assistant (rendered)\n")
            # Indent assistant markdown block to render correctly when nested
            assistant_block = text.rstrip()
            lines.append(assistant_block)
            lines.append("")
    return "\n".join(lines)
