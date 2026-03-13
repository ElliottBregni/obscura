"""obscura.cli.render — Event rendering for the interactive REPL."""

from __future__ import annotations

from typing import Any
import json
import os

from rich.console import Console
from rich.markdown import Markdown
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
        cleaned = re.sub(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)", "", cleaned)
        # DCS / PM / APC / SOS sequences
        cleaned = re.sub(r"\x1B[PX^_][^\x1B]*(?:\x1B\\|$)", "", cleaned)
        # Lone ESC + one char
        cleaned = re.sub(r"\x1B[@-Z\\-_]", "", cleaned)
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
                    Console().print(f"[dim][internal][/]{markup_escape(cleaned)}")
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

# ---- Main console: routes through sys.stdout for patch_stdout compat ----
# When prompt_toolkit's patch_stdout(raw=True) is active, sys.stdout is a
# StdoutProxy that positions output above the prompt and redraws.  By writing
# through sys.stdout (instead of a dup'd raw fd), Rich console output no
# longer overwrites the prompt while the agent is streaming.
import os as _os, sys as _sys

_real_stdout_fd = _os.dup(_sys.stdout.fileno())  # keep for fileno() queries


class _DynamicStdout:
    """File-like that delegates writes to the *current* ``sys.stdout``."""

    @property
    def encoding(self) -> str:
        return getattr(_sys.stdout, "encoding", "utf-8")

    def write(self, s: str) -> int:
        return _sys.stdout.write(s)

    def flush(self) -> None:
        _sys.stdout.flush()

    def fileno(self) -> int:
        return _real_stdout_fd

    def isatty(self) -> bool:
        return True


console = Console(file=_DynamicStdout(), force_terminal=True, legacy_windows=False)

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

    Status updates (thinking, running tool) go to a ``StreamingStatus``
    object that drives the prompt_toolkit toolbar spinner — no Rich
    ``console.status()`` / cursor manipulation that would conflict with
    ``patch_stdout``.
    """

    def __init__(self, streaming_status: object | None = None) -> None:
        self._text_buf: list[str] = []
        self._thinking_buf: list[str] = []
        self._all_text: list[str] = []
        self._thinking_blocks: list[str] = []  # completed thinking blocks
        self._in_thinking = False
        # StreamingStatus from prompt.py (toolbar spinner)
        self._ss: object | None = streaming_status

    def handle(self, event: AgentEvent) -> None:
        match event.kind:
            case AgentEventKind.TURN_START:
                self._start_thinking()

            case AgentEventKind.THINKING_DELTA:
                if not self._in_thinking:
                    self._flush_text()
                    self._in_thinking = True
                self._thinking_buf.append(event.text)
                self._update_thinking_preview()

            case AgentEventKind.TEXT_DELTA:
                self._stop_status()
                if self._in_thinking:
                    self._flush_thinking()
                self._text_buf.append(event.text)
                try:
                    self._all_text.append(event.text)
                except Exception:
                    pass

            case AgentEventKind.TOOL_CALL:
                self._stop_status()
                self._flush_all()
                self._show_tool_call(event)

            case AgentEventKind.TOOL_RESULT:
                self._stop_status()
                self._show_tool_result(event)

            case AgentEventKind.TURN_COMPLETE | AgentEventKind.AGENT_DONE:
                self._stop_status()
                self._flush_all()

            case AgentEventKind.ERROR:
                self._stop_status()
                self._flush_all()
                print_error(event.text)

            case AgentEventKind.CONTEXT_COMPACT:
                console.print(
                    f"  [yellow]⚡ {markup_escape(_sanitize_text(event.text))}[/]"
                )

            case _:
                pass

    # -- toolbar status helpers ---------------------------------------------

    def _start_thinking(self) -> None:
        if self._ss is not None:
            from obscura.cli.prompt import random_thinking_message
            self._ss.active = True  # type: ignore[attr-defined]
            self._ss.text = random_thinking_message()  # type: ignore[attr-defined]
            self._ss.preview = ""  # type: ignore[attr-defined]

    def _update_thinking_preview(self) -> None:
        if self._ss is None:
            return
        preview = "".join(self._thinking_buf).strip().replace("\n", " ")
        if len(preview) > 80:
            preview = "..." + preview[-77:]
        self._ss.preview = preview  # type: ignore[attr-defined]

    def _stop_status(self) -> None:
        if self._ss is not None:
            self._ss.active = False  # type: ignore[attr-defined]
            self._ss.text = ""  # type: ignore[attr-defined]
            self._ss.preview = ""  # type: ignore[attr-defined]

    # -- flush helpers -------------------------------------------------------

    def _flush_text(self) -> None:
        if self._text_buf:
            text = "".join(self._text_buf)
            self._text_buf.clear()
            if text.strip():
                console.print(Rule(style="dim cyan", characters="─"))
                safe_text = _sanitize_text(text)
                console.print(
                    Markdown(safe_text, code_theme=CODE_THEME),
                    soft_wrap=True,
                )

    def _flush_thinking(self) -> None:
        if self._thinking_buf:
            text = "".join(self._thinking_buf)
            self._thinking_buf.clear()
            self._in_thinking = False
            if text.strip():
                console.print()
                self._print_reasoning(text)

    def _print_reasoning(self, text: str) -> None:
        """Display reasoning as a collapsed one-liner; full text stored for Ctrl+T."""
        safe = _sanitize_text(text.strip())
        self._thinking_blocks.append(safe)
        word_count = len(safe.split())
        console.print(
            f"  [{THINKING_COLOR}]\u25b6 Thinking[/]  "
            f"[dim]({word_count} words \u2014 Ctrl+T to expand)[/]"
        )

    def get_thinking_blocks(self) -> list[str]:
        """Return all completed thinking blocks from this session."""
        return list(self._thinking_blocks)

    def get_last_thinking(self) -> str:
        """Return the most recent thinking block."""
        return self._thinking_blocks[-1] if self._thinking_blocks else ""

    def _flush_all(self) -> None:
        self._flush_thinking()
        self._flush_text()

    # -- tool display --------------------------------------------------------

    def _show_tool_call(self, event: AgentEvent) -> None:
        from obscura.cli.tool_summaries import summarize_tool_call

        name = event.tool_name
        summary = summarize_tool_call(name, event.tool_input)

        try:
            output.capture_internal(f"TOOL_CALL {name} {_sanitize_text(summary)}")
        except Exception:
            pass

        console.print(
            f"\n  [{TOOL_COLOR}]\u25b6 {markup_escape(summary)}[/]"
        )

        # Update toolbar status
        if self._ss is not None:
            self._ss.active = True  # type: ignore[attr-defined]
            self._ss.text = f"running {summary}..."  # type: ignore[attr-defined]
            self._ss.preview = ""  # type: ignore[attr-defined]

    def _show_tool_result(self, event: AgentEvent) -> None:
        raw = event.tool_result or ""
        is_err = event.is_error

        if is_err:
            err_text = _sanitize_text(raw[:200]).replace("\n", " ")
            console.print(f"  [{ERROR_COLOR}]\u2718 {markup_escape(err_text)}[/]")
            return

        # Compact success: one-line snippet
        snippet = _sanitize_text(raw[:120]).replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        console.print(f"  [dim {OK_COLOR}]\u2714 {markup_escape(snippet)}[/]")

    def finish(self) -> None:
        self._stop_status()
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
    from obscura.cli.tool_summaries import summarize_tool_call

    match event.kind:
        case AgentEventKind.TEXT_DELTA:
            console.print(_sanitize_text(event.text), end="")
        case AgentEventKind.THINKING_DELTA:
            pass  # thinking accumulated by StreamRenderer; silent in legacy path
        case AgentEventKind.TOOL_CALL:
            summary = summarize_tool_call(event.tool_name, event.tool_input)
            console.print(
                f"\n  [{TOOL_COLOR}]\u25b6 {markup_escape(summary)}[/]"
            )
        case AgentEventKind.TOOL_RESULT:
            raw = (event.tool_result or "")[:120]
            snippet = markup_escape(_sanitize_text(raw).replace("\n", " "))
            if event.is_error:
                console.print(f"  [{ERROR_COLOR}]\u2718 {snippet}[/]")
            else:
                console.print(f"  [dim {OK_COLOR}]\u2714 {snippet}[/]")
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
    from obscura.cli.app.diff_engine import DiffEngine

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
    safe_text = markup_escape(_sanitize_text(output_ev.text))
    if output_ev.is_final:
        console.print(f"  [bold {ACCENT}]{markup_escape(_sanitize_text(output_ev.agent_name))}:[/] {safe_text}")
    else:
        console.print(safe_text, end="")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _banner_overhaul_block(solid_color: str | None = None) -> None:
    """Print OVERHAUL block letters with a detailed black cat.

    Oscillates Irish green <-> dark blue by default.
    Pass solid_color (raw ANSI escape str) to use a single colour instead.
    """
    import sys

    RESET = "\033[0m"
    BOLD  = "\033[1m"
    GREEN = "\033[38;5;28m"   # Irish green
    BLUE  = "\033[38;5;17m"   # deep dark navy blue
    GRAY  = "\033[38;5;240m"
    CAT_BLK  = "\033[38;5;232m"   # near-black for cat body
    CAT_GRY  = "\033[38;5;236m"   # dark gray shading
    CAT_NOSE = "\033[38;5;175m"   # pink nose
    CAT_WHSK = "\033[38;5;250m"   # light gray whiskers
    CAT_EYE  = "\033[38;5;46m"    # bright green eyes

    lines = [
        "  \u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2557   \u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2557  \u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2557   \u2588\u2588\u2557\u2588\u2588\u2557",
        " \u2588\u2588\u2554\u2550\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2551",
        " \u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2551",
        " \u2588\u2588\u2551   \u2588\u2588\u2551\u255a\u2588\u2588\u2557 \u2588\u2588\u2554\u255d\u2588\u2588\u2554\u2550\u2550\u255d  \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2551\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2551",
        " \u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d \u255a\u2588\u2588\u2588\u2588\u2554\u255d \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2551  \u2588\u2588\u2551\u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557",
        "  \u255a\u2550\u2550\u2550\u2550\u2550\u255d   \u255a\u2550\u2550\u2550\u255d  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d",
    ]
    # ── Two cats peeking over the OVERHAUL text ──────────────────
    B = CAT_BLK
    G = CAT_GRY
    E = CAT_EYE
    N = CAT_NOSE
    W = CAT_WHSK
    R = RESET

    g = " " * 36          # gap between the two cats
    g2 = " " * 34
    g3 = " " * 32
    g4 = " " * 30

    cat_above = [
        f"  {B}       /\\     /\\{g}/\\     /\\{R}",
        f"  {B}      /  \\   /  \\{g2}/  \\   /  \\{R}",
        f"  {B}     /    \\_/    \\{g3}/    \\_/    \\{R}",
        f"  {B}    |  {E}o{B}       {E}o{B}  |{g4}|  {E}o{B}       {E}o{B}  |{R}",
        f"  {W}  ~~{B}|{W}    {N}/^\\{W}    {B}|{W}~~{'~' * 30}~~{B}|{W}    {N}/^\\{W}    {B}|{W}~~{R}",
        f"  {W}    {B}|{G}   ( Y )   {B}|{R}{' ' * 30}  {B}|{G}   ( Y )   {B}|{R}",
        f"  {G}____|{B}/{G}  \\   /  {B}\\{G}|{'_' * 30}|{B}/{G}  \\   /  {B}\\{G}|____{R}",
    ]
    sys.stdout.write("\n")
    for cline in cat_above:
        sys.stdout.write(f"  {cline}\n")

    # OVERHAUL block text
    for i, line in enumerate(lines):
        c = solid_color if solid_color else (GREEN if i % 2 == 0 else BLUE)
        sys.stdout.write(f"  {BOLD}{c}{line}{RESET}\n")
    sys.stdout.write(f"\n{GRAY}  {chr(0x2501) * 66}{RESET}\n")
    sys.stdout.flush()


def _banner_obscura_by_overhaul() -> None:
    """Print OBSCURA (cyan/teal oscillating) then 'by OVERHAUL' (green/blue)."""
    import sys

    RESET = "\033[0m"
    BOLD  = "\033[1m"
    CYAN  = "\033[38;5;51m"
    TEAL  = "\033[38;5;37m"
    GREEN = "\033[38;5;35m"
    GRAY  = "\033[38;5;240m"

    obscura_lines = [
        "  \u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2557   \u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2588\u2588\u2588\u2557 ",
        " \u2588\u2588\u2554\u2550\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557",
        " \u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2551     \u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551",
        " \u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u255a\u2550\u2550\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2551     \u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2551",
        " \u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551\u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2551  \u2588\u2588\u2551",
        "  \u255a\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u255d  \u255a\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d",
    ]
    sys.stdout.write("\n")
    for i, line in enumerate(obscura_lines):
        c = CYAN if i % 2 == 0 else TEAL
        sys.stdout.write(f"  {BOLD}{c}{line}{RESET}\n")

    by_line = "         by  O V E R H A U L"
    sys.stdout.write(f"\n  {BOLD}{GREEN}{by_line}{RESET}\n")
    sys.stdout.write(f"\n{GRAY}  {chr(0x2501) * 60}{RESET}\n")
    sys.stdout.flush()


def _obscura_ascii_banner() -> None:
    """Print the startup ASCII art banner — theme driven by FLAGS.banner_theme."""
    try:
        from obscura.core.feature_flags import FLAGS, BannerTheme

        if not FLAGS.banner_enabled or FLAGS.banner_theme == BannerTheme.NONE:
            return

        if FLAGS.banner_theme == BannerTheme.OVERHAUL_GREEN_BLUE:
            _banner_overhaul_block()
            return

        if FLAGS.banner_theme == BannerTheme.OVERHAUL_ORANGE:
            _banner_overhaul_block(solid_color="\033[38;5;208m")
            return

        if FLAGS.banner_theme == BannerTheme.OBSCURA_BY_OVERHAUL:
            _banner_obscura_by_overhaul()
            return

    except Exception:
        pass  # fall through to default if feature_flags import fails

    # OBSCURA_DEFAULT — original oscillating purple/blue wave
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
    available_agents: list[str] | None = None,
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
    if available_agents:
        console.print(
            f"  [bold]agents:[/]    [{ACCENT}]{', '.join(available_agents)}[/]   "
            "[dim]/agent spawn <name> or @name <prompt>[/]"
        )
    console.print()
    console.print(f"  [dim]Type [bold]/help[/bold] for commands, [bold]/quit[/bold] to exit.[/]")
    console.print()


def print_error(msg: str) -> None:
    """Print an error message."""
    console.print(f"[bold {ERROR_COLOR}]  error:[/] {markup_escape(msg)}")


def print_info(msg: str) -> None:
    """Print an info message."""
    console.print(f"[dim {ACCENT_DIM}]  {markup_escape(msg)}[/]")


def print_warning(msg: str) -> None:
    """Print a warning message."""
    console.print(f"[{WARN_COLOR}]  {markup_escape(msg)}[/]")


def print_ok(msg: str) -> None:
    """Print a success message."""
    console.print(f"[bold {OK_COLOR}]  {markup_escape(msg)}[/]")


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
