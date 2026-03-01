"""obscura.cli.render — Event rendering for the interactive REPL."""

from __future__ import annotations

from typing import Any
import json
import os

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as markup_escape
from rich.markup import escape as markup_escape
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.rule import Rule
from rich.table import Table
from datetime import datetime

from obscura.core.types import AgentEvent, AgentEventKind

import re


# ---------------------------------------------------------------------------
# Theme constants
# ---------------------------------------------------------------------------

ACCENT = "bright_cyan"
ACCENT_DIM = "cyan"
TOOL_COLOR = "bright_yellow"
THINKING_COLOR = "bright_magenta"
ERROR_COLOR = "bright_red"
OK_COLOR = "bright_green"
WARN_COLOR = "yellow"
CODE_THEME = "monokai"


def _sanitize_text(s: str) -> str:
    """Remove ANSI/escape sequences and control characters from text."""
    if not s:
        return ""
    try:
        # CSI sequences: ESC [ ... final-byte
        cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", s)
        # OSC sequences: ESC ] ... (ST or BEL)
        cleaned = re.sub(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\\\)", "", cleaned)
        # DCS / PM / APC / SOS sequences
        cleaned = re.sub(r"\x1B[PX^_][^\x1B]*(?:\x1B\\\\|$)", "", cleaned)
        # Lone ESC + one char
        cleaned = re.sub(r"\x1B[@-Z\\\\-_]", "", cleaned)
        # Bare ESC
        cleaned = re.sub(r"\x1B", "", cleaned)
        # C0 controls (keep TAB \x09, LF \x0A, CR \x0D)
        cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+", "", cleaned)
        return cleaned
    except Exception:
        return s
def _detect_language(text: str) -> str | None:
    """Try to detect language from content for syntax highlighting."""
    stripped = text.strip()
    if not stripped:
        return None
    # JSON
    if (stripped.startswith("{") and stripped.endswith("}")) or \
       (stripped.startswith("[") and stripped.endswith("]")):
        try:
            json.loads(stripped)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
    # TOML
    if re.match(r'^\[[\w.]+\]', stripped, re.MULTILINE):
        return "toml"
    # YAML
    if re.match(r'^[\w_]+:\s', stripped) and '\n' in stripped:
        return "yaml"
    # Python
    if re.match(r'^(def |class |import |from |if __name__|async def )', stripped):
        return "python"
    # JavaScript/TypeScript
    if re.match(r'^(const |let |var |function |export |import )', stripped):
        return "javascript"
    # SQL
    if re.match(r'^(SELECT |INSERT |UPDATE |DELETE |CREATE |ALTER |DROP )', stripped, re.IGNORECASE):
        return "sql"
    # Shell
    if stripped.startswith("$ ") or stripped.startswith("#!"):
        return "bash"
    return None


def _render_structured(text: str) -> Syntax | None:
    """If text looks like code/JSON/TOML, return a Syntax object."""
    lang = _detect_language(text)
    if lang:
        try:
            return Syntax(
                text.strip(),
                lang,
                theme=CODE_THEME,
                line_numbers=False,
                word_wrap=True,
                padding=(0, 1),
            )
        except Exception:
            pass
    return None


class OutputManager:
    """Lightweight output manager for routing internal debug and prints."""

    def __init__(self, env: str = "cli", verbose_internals: bool = False) -> None:
        self.env = env
        self.verbose = verbose_internals
        self._buffer: list[str] = []

    def capture_internal(self, message: str) -> None:
        if not self.verbose:
            return
        try:
            self._buffer.append(message)
        except Exception:
            pass

        try:
            cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", message)
        except Exception:
            cleaned = message

        if self.env == "cli":
            try:
                Console().print(Text.from_ansi(cleaned))
            except Exception:
                try:
                    Console().print(f"[dim][internal][/]{cleaned}")
                except Exception:
                    pass
        else:
            try:
                if self._buffer:
                    self._buffer[-1] = cleaned
            except Exception:
                pass

    def get_buffer(self) -> list[str]:
        return list(self._buffer)


from obscura import config

output = OutputManager(env=config.OUTPUT_MODE, verbose_internals=config.VERBOSE)

if config.CAPTURE_PRINTS:
    import builtins

    _orig_print = builtins.print

    def _capturing_print(*args, **kwargs):
        _orig_print(*args, **kwargs)
        try:
            output.capture_internal(" ".join(str(a) for a in args))
        except Exception:
            pass

    builtins.print = _capturing_print

# ---- Main console: COLOR ENABLED ----
# Preserve the real stdout fd before prompt_toolkit's patch_stdout() can
# replace sys.stdout with a StdoutProxy.  Rich resolves sys.stdout at
# print-time, so without this the ANSI escapes get mangled through the proxy.
import os as _os, sys as _sys

_real_stdout_fd = _os.dup(_sys.stdout.fileno())
_real_stdout = _os.fdopen(_real_stdout_fd, "w", closefd=False)

console = Console(file=_real_stdout, force_terminal=True, legacy_windows=False)

# Active renderer for expand-preview hotkey (set by send_message)
_active_renderer: "StreamRenderer" | None = None

def set_active_renderer(r: "StreamRenderer" | None) -> None:
    global _active_renderer
    _active_renderer = r

def get_active_text() -> str:
    try:
        if _active_renderer is None:
            return ""
        return _active_renderer.get_accumulated_text()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# StreamRenderer
# ---------------------------------------------------------------------------


class StreamRenderer:
    """Accumulates text deltas and renders as Markdown on flush.

    Shows a visible spinner while the model is busy, syntax-highlights
    code/JSON/TOML blocks, and renders all text as rich Markdown.
    """

    def __init__(self, external_status: object | None = None) -> None:
        self._text_buf: list[str] = []
        self._thinking_buf: list[str] = []
        self._all_text: list[str] = []
        self._in_thinking = False
        self._status: object | None = None
        self._external_status: object | None = external_status
        self._thinking_status: object | None = None  # visible busy spinner

    def handle(self, event: AgentEvent) -> None:
        match event.kind:
            case AgentEventKind.TURN_START:
                self._start_thinking_spinner()

            case AgentEventKind.THINKING_DELTA:
                if not self._in_thinking:
                    self._flush_text()
                    self._in_thinking = True
                self._thinking_buf.append(event.text)
                # Update spinner with live preview
                self._update_thinking_preview(event.text)

            case AgentEventKind.TEXT_DELTA:
                self._stop_thinking_spinner()
                if self._in_thinking:
                    self._flush_thinking()
                self._text_buf.append(event.text)
                try:
                    self._all_text.append(event.text)
                except Exception:
                    pass

            case AgentEventKind.TOOL_CALL:
                self._stop_thinking_spinner()
                self._flush_all()
                self._show_tool_call(event)

            case AgentEventKind.TOOL_RESULT:
                self._stop_spinner()
                self._show_tool_result(event)

            case AgentEventKind.TURN_COMPLETE | AgentEventKind.AGENT_DONE:
                self._stop_thinking_spinner()
                self._flush_all()

            case AgentEventKind.ERROR:
                self._stop_thinking_spinner()
                self._flush_all()
                print_error(event.text)

            case AgentEventKind.CONTEXT_COMPACT:
                console.print(
                    f"  [yellow]⚡ {event.text}[/]"
                )

            case _:
                pass

    # -- thinking spinner ---------------------------------------------------

    def _start_thinking_spinner(self) -> None:
        """Show a visible spinner while the model is working."""
        if self._external_status is not None:
            try:
                self._external_status.update(  # type: ignore[attr-defined]
                    f"[{THINKING_COLOR}]  thinking...[/]"
                )
            except Exception:
                pass
            return
        if self._thinking_status is None:
            try:
                self._thinking_status = console.status(
                    f"[bold {THINKING_COLOR}]  thinking...[/]",
                    spinner="dots",
                    spinner_style=THINKING_COLOR,
                )
                self._thinking_status.start()  # type: ignore[union-attr]
            except Exception:
                self._thinking_status = None

    def _update_thinking_preview(self, delta: str) -> None:
        """Update the spinner label with a snippet of the model's thinking."""
        preview = "".join(self._thinking_buf).strip().replace("\n", " ")
        if len(preview) > 80:
            preview = "..." + preview[-77:]
        label = f"[bold {THINKING_COLOR}]  thinking[/] [{THINKING_COLOR} dim]{preview}[/]"
        if self._external_status is not None:
            try:
                self._external_status.update(label)  # type: ignore[attr-defined]
            except Exception:
                pass
        elif self._thinking_status is not None:
            try:
                self._thinking_status.update(label)  # type: ignore[union-attr]
            except Exception:
                pass

    def _stop_thinking_spinner(self) -> None:
        if self._thinking_status is not None:
            try:
                self._thinking_status.stop()  # type: ignore[union-attr]
            except Exception:
                pass
            self._thinking_status = None

    # -- flush helpers -------------------------------------------------------

    def _flush_text(self) -> None:
        if self._text_buf:
            text = "".join(self._text_buf)
            self._text_buf.clear()
            if text.strip():
                console.print()
                safe_text = _sanitize_text(text)
                console.print(
                    Markdown(safe_text, code_theme=CODE_THEME),
                    soft_wrap=True,
                )
                if self._external_status is not None:
                    try:
                        ts = datetime.now().strftime("%H:%M:%S")
                        preview = safe_text.strip().replace('\n', ' ')[:120]
                        self._external_status.update(  # type: ignore[attr-defined]
                            f"[{ACCENT_DIM}]  assistant [{ts}]: {preview}[/]"
                        )
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
                        self._external_status.update(  # type: ignore[attr-defined]
                            f"[{THINKING_COLOR}]  thinking [{ts}]: {preview}[/]"
                        )
                    except Exception:
                        console.print()
                        self._print_reasoning(text)
                else:
                    console.print()
                    self._print_reasoning(text)

    def _print_reasoning(self, text: str) -> None:
        """Display reasoning/thinking in a subtle bordered panel."""
        safe = _sanitize_text(text.strip())
        console.print(
            Panel(
                Text(safe, style="dim italic"),
                title=f"[{THINKING_COLOR}]reasoning[/]",
                title_align="left",
                border_style="dim magenta",
                expand=False,
                padding=(0, 1),
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
            parts.append(f"[dim]{k}=[/]{sv}")
        arg_str = ", ".join(parts)
        if len(arg_str) > 140:
            arg_str = arg_str[:137] + "..."

        sanitized_args = _sanitize_text(arg_str)
        try:
            output.capture_internal(f"TOOL_CALL {name} {_sanitize_text(', '.join(parts))}")
        except Exception:
            pass

        # Tool call: icon + name + args on one line
        console.print(
            f"\n  [{TOOL_COLOR}]  {markup_escape(name)}[/]  [dim]{markup_escape(sanitized_args)}[/]"
        )

        if self._external_status is not None:
            try:
                ts = datetime.now().strftime("%H:%M:%S")
                self._external_status.update(  # type: ignore[attr-defined]
                    f"[{TOOL_COLOR}]  running {name}...[/]"
                )
            except Exception:
                pass

        try:
            if self._external_status is None:
                self._status = console.status(
                    f"  [dim {TOOL_COLOR}]  running...[/]",
                    spinner="dots",
                    spinner_style=TOOL_COLOR,
                )
                self._status.start()  # type: ignore[union-attr]
        except Exception:
            self._status = None

    def _show_tool_result(self, event: AgentEvent) -> None:
        raw = event.tool_result or ""
        is_err = event.is_error

        if is_err:
            console.print(f"  [{ERROR_COLOR}]  {markup_escape(_sanitize_text(raw[:200]))}[/]")
            return

        # Try to syntax-highlight structured output (JSON, TOML, code)
        snippet = raw[:2000]
        highlighted = _render_structured(snippet)
        if highlighted is not None:
            console.print(
                Panel(
                    highlighted,
                    border_style="dim green",
                    expand=False,
                    padding=(0, 0),
                )
            )
        else:
            # Plain result: show as dim text, truncated
            short = markup_escape(_sanitize_text(raw[:300]))
            console.print(f"  [dim {OK_COLOR}]  {short}[/]")

        if self._external_status is not None:
            try:
                ts = datetime.now().strftime("%H:%M:%S")
                preview = _sanitize_text(raw[:80]).replace('\n', ' ')
                self._external_status.update(  # type: ignore[attr-defined]
                    f"[{ACCENT_DIM}]  done [{ts}]: {preview}[/]"
                )
            except Exception:
                pass

    def _stop_spinner(self) -> None:
        if self._status is not None:
            try:
                self._status.stop()  # type: ignore[union-attr]
            except Exception:
                pass
            self._status = None
        if self._external_status is not None:
            try:
                self._external_status.update("")  # type: ignore[attr-defined]
            except Exception:
                pass

    def finish(self) -> None:
        self._stop_thinking_spinner()
        self._flush_all()

    def get_accumulated_text(self) -> str:
        try:
            parts: list[str] = []
            parts.extend(self._all_text)
            parts.extend(self._text_buf)
            parts.extend(self._thinking_buf)
            return "".join(parts)
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# LabeledStreamRenderer
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
                        Rule(
                            f"[bold {self._color}]{self._label}[/]",
                            style=self._color,
                        )
                    )
                    self._header_printed = True
                console.print(
                    Markdown(text, code_theme=CODE_THEME),
                    soft_wrap=True,
                )


# ---------------------------------------------------------------------------
# Legacy render_event
# ---------------------------------------------------------------------------


def render_event(event: AgentEvent) -> None:
    """Simple single-event renderer (no Markdown accumulation)."""
    match event.kind:
        case AgentEventKind.TEXT_DELTA:
            console.print(_sanitize_text(event.text), end="")
        case AgentEventKind.THINKING_DELTA:
            safe = markup_escape(_sanitize_text(event.text))
            console.print(f"[dim italic {THINKING_COLOR}]{safe}[/]", end="")
        case AgentEventKind.TOOL_CALL:
            console.print(
                f"\n  [{TOOL_COLOR}]  {markup_escape(_sanitize_text(event.tool_name))}[/]"
            )
        case AgentEventKind.TOOL_RESULT:
            snippet = markup_escape(_sanitize_text((event.tool_result or "")[:120]))
            style = ERROR_COLOR if event.is_error else "dim green"
            prefix = "" if event.is_error else ""
            console.print(f"  [{style}]{prefix} {snippet}[/]")
        case _:
            pass


# ---------------------------------------------------------------------------
# Plan / Diff rendering
# ---------------------------------------------------------------------------


def render_plan(plan: Any) -> None:
    """Render a Plan with step statuses."""
    console.print()
    console.print(Rule(f"[bold]{plan.title}[/]", style=ACCENT_DIM))

    tbl = Table(show_header=False, box=None, padding=(0, 1), expand=False)
    tbl.add_column("status", width=3)
    tbl.add_column("num", style=ACCENT, width=4)
    tbl.add_column("desc")

    for step in plan.steps:
        if step.status == "approved":
            icon = f"[{OK_COLOR}][/]"
        elif step.status == "rejected":
            icon = f"[{ERROR_COLOR}][/]"
        elif step.status == "edited":
            icon = f"[{WARN_COLOR}][/]"
        else:
            icon = "[dim][/]"
        tbl.add_row(icon, f"{step.number}.", step.description)

    console.print(tbl)
    console.print(
        f"\n[dim]{plan.approved_count} approved  {plan.rejected_count} rejected  "
        f"{plan.pending_count} pending[/]"
    )
    if plan.all_decided:
        console.print(f"[{OK_COLOR}]All steps decided. /mode code to execute.[/]")
    else:
        console.print("[dim]/approve <n|all>  /reject <n|all>[/]")


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
        console.print(
            f"\n[bold {ACCENT}]{fc['path']}[/]  [dim]({len(diff_fc.hunks)} hunks)[/]"
        )
        unified = engine.format_unified(diff_fc)
        if unified.strip():
            try:
                console.print(Syntax(unified, "diff", theme=CODE_THEME))
            except Exception:
                console.print(unified)
        for hunk in diff_fc.hunks:
            status_icon = {"accepted": "", "rejected": ""}.get(
                hunk.status, ""
            )
            console.print(
                f"  [dim]hunk {hunk_idx}: "
                f"@@ -{hunk.old_start},{hunk.old_count} "
                f"+{hunk.new_start},{hunk.new_count} @@ "
                f"{status_icon}[/]"
            )
            hunk_idx += 1
    console.print(f"\n[dim]/diff accept <n|all>  /diff reject <n|all>  /diff apply[/]")


# ---------------------------------------------------------------------------
# InteractionBus rendering
# ---------------------------------------------------------------------------


def render_attention_request(request: Any) -> None:
    """Render an AttentionRequest from the InteractionBus."""
    priority_style = {
        "low": "dim",
        "normal": WARN_COLOR,
        "high": f"bold {WARN_COLOR}",
        "critical": f"bold {ERROR_COLOR}",
    }
    pri = getattr(request.priority, "value", "normal")
    style = priority_style.get(pri, WARN_COLOR)

    console.print(
        Panel(
            Text(request.message),
            title=f"[{style}] {request.agent_name}[/]",
            subtitle=f"[dim]{request.request_id[:12]}[/]",
            border_style=style,
            expand=False,
        )
    )
    actions = request.actions
    if actions and actions != ("ok",):
        console.print(f"  [dim]Actions: {', '.join(actions)}[/]")


def render_agent_output(output_ev: Any) -> None:
    """Render an AgentOutput from the InteractionBus."""
    if not output_ev.text:
        return
    safe_text = _sanitize_text(output_ev.text)
    if output_ev.is_final:
        console.print(f"  [bold {ACCENT}]{markup_escape(_sanitize_text(output_ev.agent_name))}:[/] {markup_escape(safe_text)}")
    else:
        console.print(markup_escape(safe_text), end="")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _obscura_ascii_banner() -> None:
    """Print the oscillating OBSCURA ASCII art banner to stdout."""
    letters = {
        "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
        "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
        "S": [" ####", "#    ", " ### ", "    #", "#### "],
        "C": [" ####", "#    ", "#    ", "#    ", " ####"],
        "U": ["#   #", "#   #", "#   #", "#   #", " ### "],
        "R": ["#### ", "#   #", "#### ", "#  # ", "#   #"],
        "A": ["  #  ", " # # ", "#####", "#   #", "#   #"],
    }
    rows: list[str] = ["", "", "", "", ""]
    for ch in "OBSCURA":
        p = letters[ch]
        for i in range(5):
            rows[i] += p[i] + "  "

    WAVE = [
        "\033[38;5;129m",
        "\033[38;5;99m",
        "\033[38;5;63m",
        "\033[38;5;33m",
        "\033[38;5;39m",
        "\033[38;5;51m",
        "\033[38;5;39m",
        "\033[38;5;33m",
        "\033[38;5;63m",
        "\033[38;5;99m",
    ]
    RESET = "\033[0m"
    BOLD = "\033[1m"

    import sys
    sys.stdout.write("\n")
    for row_idx, line in enumerate(rows):
        colored = BOLD
        for col_idx, ch in enumerate(line):
            color_idx = (col_idx // 2 + row_idx) % len(WAVE)
            colored += WAVE[color_idx] + ch
        colored += RESET
        sys.stdout.write("  " + colored + "\n")
    sys.stdout.write(f"\n\033[38;5;99m  {'─' * 41}{RESET}\n")
    sys.stdout.flush()


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
    _obscura_ascii_banner()

    label = backend
    if model:
        label += f", {model}"

    info_parts: list[str] = []
    if tool_count:
        info_parts.append(f"[{TOOL_COLOR}]{tool_count} tools[/]")
    if mcp_servers:
        info_parts.append(f"[{ACCENT}]MCP: {', '.join(mcp_servers)}[/]")
    info_parts.append(f"mode: [bold]{mode}[/]")
    info_line = "  ".join(info_parts)

    console.print(f"  [bold]model:[/]     [{ACCENT}]{model or 'default'}[/]   [dim]/model to change[/]")
    console.print(f"  [bold]backend:[/]   [{ACCENT}]{label}[/]")
    if info_line:
        console.print(f"  {info_line}")
    console.print()
    console.print(f"  [dim]Type [bold]/help[/bold] for commands, [bold]/quit[/bold] to exit.[/]")
    console.print()


def print_error(msg: str) -> None:
    """Print an error message."""
    console.print(f"[bold {ERROR_COLOR}]  error:[/] {msg}")


def print_info(msg: str) -> None:
    """Print an info message."""
    console.print(f"[dim {ACCENT_DIM}]  {msg}[/]")


def print_warning(msg: str) -> None:
    """Print a warning message."""
    console.print(f"[{WARN_COLOR}]  {msg}[/]")


def print_ok(msg: str) -> None:
    """Print a success message."""
    console.print(f"[bold {OK_COLOR}]  {msg}[/]")


# ---------------------------------------------------------------------------
# Markdown transcript export helper
# ---------------------------------------------------------------------------

def export_transcript_markdown(history: list[tuple[str, str]]) -> str:
    """Export a conversation history to a Markdown-formatted transcript."""
    lines: list[str] = []
    lines.append("# Conversation transcript\n")
    for idx, (role, text) in enumerate(history, start=1):
        if role == "user":
            lines.append(f"### {idx}. User\n")
            lines.append("```\n" + text.strip() + "\n```")
            lines.append("")
        else:
            lines.append(f"> Assistant (rendered)\n")
            assistant_block = text.rstrip()
            lines.append(assistant_block)
            lines.append("")
    return "\n".join(lines)
