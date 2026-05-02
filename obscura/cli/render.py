"""obscura.cli.render — Event rendering for the interactive REPL."""

from __future__ import annotations

import json
import re
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from obscura.core.types import AgentEvent, AgentEventKind

# ---------------------------------------------------------------------------
# Figures (matching Claude Code's visual language)
# ---------------------------------------------------------------------------

import platform as _platform

BLACK_CIRCLE = "⏺" if _platform.system() == "Darwin" else "●"
BULLET = "∙"
CHECK_MARK = "✓"
CROSS_MARK = "✗"
BLOCKQUOTE_BAR = "▎"
HEAVY_HORIZONTAL = "━"
LIGHTNING_BOLT = "↯"


# ---------------------------------------------------------------------------
# Theme constants — sourced from Catppuccin Mocha palette
# ---------------------------------------------------------------------------

from obscura.cli.renderer.modern.theme import (
    ERROR_HEX,
    OK_HEX,
    THINKING_HEX,
    TOOL_HEX,
    WARN_HEX,
    BLUE,
    SAPPHIRE,
)

ACCENT = BLUE.hex
ACCENT_DIM = SAPPHIRE.hex
TOOL_COLOR = TOOL_HEX
THINKING_COLOR = THINKING_HEX
ERROR_COLOR = ERROR_HEX
OK_COLOR = OK_HEX
WARN_COLOR = WARN_HEX
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
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+", "", cleaned)
    except Exception:
        return s


def _detect_language(text: str) -> str | None:
    """Try to detect language from content for syntax highlighting."""
    stripped = text.strip()
    if not stripped:
        return None
    # JSON
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        try:
            json.loads(stripped)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
    # TOML
    if re.match(r"^\[[\w.]+\]", stripped, re.MULTILINE):
        return "toml"
    # YAML
    if re.match(r"^[\w_]+:\s", stripped) and "\n" in stripped:
        return "yaml"
    # Python
    if re.match(r"^(def |class |import |from |if __name__|async def )", stripped):
        return "python"
    # JavaScript/TypeScript
    if re.match(r"^(const |let |var |function |export |import )", stripped):
        return "javascript"
    # SQL
    if re.match(
        r"^(SELECT |INSERT |UPDATE |DELETE |CREATE |ALTER |DROP )",
        stripped,
        re.IGNORECASE,
    ):
        return "sql"
    # Shell
    if stripped.startswith(("$ ", "#!")):
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
    """Lightweight output manager for routing internal debug and prints.

    Backwards-compatible: accepts optional log_level kw used by tests.
    """

    def __init__(
        self,
        env: str = "cli",
        verbose_internals: bool = False,
        log_level: str | None = None,
    ) -> None:
        self.env = env
        self.verbose = verbose_internals
        self.log_level = log_level or "info"
        self._buffer: list[str] = []
        self._session_log_path = None

    def configure_session_log_path(self, path) -> None:
        """Configure directory where session hidden deltas will be stored.

        Expects a pathlib.Path or str directory. Creates a file named
        'hidden_deltas.log' in the provided directory.
        """
        try:
            from pathlib import Path

            p = Path(path)
            p.mkdir(parents=True, exist_ok=True)
            file_path = p / "hidden_deltas.log"
            # Touch the file
            file_path.write_text("", encoding="utf-8")
            self._session_log_path = file_path
        except Exception:
            self._session_log_path = None

    def capture_hidden_delta(self, kind: str, text: str, *, status: str = "") -> None:
        """Append a hidden delta JSON line to the configured session log."""
        try:
            if self._session_log_path is None:
                return
            import json
            import time

            row = {"kind": kind, "status": status, "text": text, "ts": time.time()}
            with open(self._session_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # Mirror to internal buffer depending on log level (medium suppresses noisy deltas)
        try:
            if getattr(self, "log_level", None) != "medium":
                self._buffer.append(f"{kind} {text}")
        except Exception:
            pass

    def set_log_level(self, level: str) -> None:
        """Set the internal log level used to decide what to mirror to buffer."""
        try:
            self.log_level = level
        except Exception:
            pass

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


import contextlib

from obscura import config

output = OutputManager(env=config.OUTPUT_MODE, verbose_internals=config.VERBOSE)

if config.CAPTURE_PRINTS:
    import builtins

    _orig_print = builtins.print

    def _capturing_print(*args, **kwargs) -> None:
        _orig_print(*args, **kwargs)
        with contextlib.suppress(Exception):
            output.capture_internal(" ".join(str(a) for a in args))

    builtins.print = _capturing_print

# ---- Main console: routes through sys.stdout for patch_stdout compat ----
# When prompt_toolkit's patch_stdout(raw=True) is active, sys.stdout is a
# StdoutProxy that positions output above the prompt and redraws.  By writing
# through sys.stdout (instead of a dup'd raw fd), Rich console output no
# longer overwrites the prompt while the agent is streaming.
import os as _os
import sys as _sys

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
_active_renderer: Any = None


def set_active_renderer(r: Any) -> None:
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

    def __init__(
        self,
        streaming_status: object | None = None,
        external_status: object | None = None,
    ) -> None:
        """Constructor accepts both streaming_status and external_status (tests pass external_status).

        The renderer is tolerant of StreamingStatus objects that either expose
        attributes (active/text/preview) or provide an update(payload) method.
        """
        self._text_buf: list[str] = []
        self._thinking_buf: list[str] = []
        self._all_text: list[str] = []
        self._thinking_blocks: list[str] = []  # completed thinking blocks
        self._in_thinking = False
        # StreamingStatus from prompt.py (toolbar spinner)
        self._ss: object | None = external_status or streaming_status
        # jitter control for reasoning preview
        self._last_preview_update: float = float("-inf")
        import os as _os

        self._jitter_ms = int(_os.environ.get("OBSCURA_REASONING_JITTER_MS", "50"))

    def handle(self, event: AgentEvent) -> None:
        match event.kind:
            case AgentEventKind.TURN_START:
                self._start_thinking()

            case AgentEventKind.THINKING_DELTA:
                if not self._in_thinking:
                    self._flush_text()
                    self._in_thinking = True
                self._thinking_buf.append(event.text)
                # Persist hidden reasoning deltas to session log for later replay
                try:
                    status = None
                    if hasattr(event, "raw") and isinstance(event.raw, dict):
                        status = event.raw.get("status", "")
                    output.capture_hidden_delta(
                        "REASONING_DELTA",
                        event.text,
                        status=status or "",
                    )
                except Exception:
                    pass
                self._update_thinking_preview(getattr(event, "raw", None))

            case AgentEventKind.TEXT_DELTA:
                self._stop_status()
                if self._in_thinking:
                    self._flush_thinking()
                self._text_buf.append(event.text)
                with contextlib.suppress(Exception):
                    self._all_text.append(event.text)

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
                    f"  [{WARN_COLOR}]{LIGHTNING_BOLT} {markup_escape(_sanitize_text(event.text))}[/]",
                )

            case _:
                pass

    # -- toolbar status helpers ---------------------------------------------

    def _start_thinking(self) -> None:
        if self._ss is None:
            return
        from obscura.cli.prompt import random_thinking_message

        msg = random_thinking_message()
        try:
            # Prefer update(payload) API if available
            if hasattr(self._ss, "update"):
                self._ss.update({"active": True, "text": msg, "preview": ""})
            else:
                self._ss.active = True  # type: ignore[attr-defined]
                self._ss.text = msg  # type: ignore[attr-defined]
                self._ss.preview = ""  # type: ignore[attr-defined]
        except Exception:
            # best-effort
            try:
                self._ss.active = True  # type: ignore[attr-defined]
            except Exception:
                pass

    def _update_thinking_preview(self, raw_status: dict | None = None) -> None:
        """Update toolbar preview, but respect jitter so updates are rate-limited."""
        if self._ss is None:
            return
        try:
            import time

            now = time.monotonic()
            ms_elapsed = (now - self._last_preview_update) * 1000.0
            if ms_elapsed < self._jitter_ms:
                return
            preview = "".join(self._thinking_buf).strip().replace("\n", " ")
            if len(preview) > 80:
                preview = "..." + preview[-77:]
            payload = {"preview": preview}
            if raw_status and isinstance(raw_status, dict) and "status" in raw_status:
                payload["status"] = raw_status.get("status")
            if hasattr(self._ss, "update"):
                try:
                    self._ss.update(payload)
                except Exception:
                    # fallback to attribute assignment
                    try:
                        self._ss.preview = preview  # type: ignore[attr-defined]
                    except Exception:
                        pass
            else:
                try:
                    self._ss.preview = preview  # type: ignore[attr-defined]
                    if (
                        raw_status
                        and isinstance(raw_status, dict)
                        and "status" in raw_status
                    ):
                        self._ss.text = raw_status.get("status")  # type: ignore[attr-defined]
                except Exception:
                    pass
            self._last_preview_update = now
        except Exception:
            pass

    def _stop_status(self) -> None:
        if self._ss is None:
            return
        try:
            if hasattr(self._ss, "update"):
                try:
                    self._ss.update({"active": False, "text": "", "preview": ""})
                except Exception:
                    # fallback to attribute assignment
                    self._ss.active = False  # type: ignore[attr-defined]
                    self._ss.text = ""  # type: ignore[attr-defined]
                    self._ss.preview = ""  # type: ignore[attr-defined]
            else:
                self._ss.active = False  # type: ignore[attr-defined]
                self._ss.text = ""  # type: ignore[attr-defined]
                self._ss.preview = ""  # type: ignore[attr-defined]
        except Exception:
            pass

    # -- flush helpers -------------------------------------------------------

    def _flush_text(self) -> None:
        if self._text_buf:
            text = "".join(self._text_buf)
            self._text_buf.clear()
            if text.strip():
                safe_text = _sanitize_text(text)
                console.print()
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
        """Display reasoning inline — compact blockquote style like Claude Code."""
        safe = _sanitize_text(text.strip())
        self._thinking_blocks.append(safe)

        # Compact display: blockquote bar + dimmed text (no bordered Panel)
        lines = safe.split("\n")
        # Show first few lines, collapse the rest
        max_preview = 4
        if len(lines) <= max_preview:
            preview = safe
        else:
            preview = (
                "\n".join(lines[:max_preview])
                + f"\n... ({len(lines) - max_preview} more lines)"
            )

        # Render as dimmed blockquote with left bar
        for line in preview.split("\n"):
            console.print(
                f"  [{THINKING_COLOR}]{BLOCKQUOTE_BAR}[/] [dim italic]{markup_escape(line)}[/]",
            )

    @staticmethod
    def _normalize_reasoning_text(raw: str) -> str:
        """Normalize multi-line reasoning into cleaned paragraphs.

        Joins consecutive non-empty lines with a single space, preserves
        paragraph breaks as double-newlines, and trims extra whitespace.
        """
        try:
            lines = [ln.strip() for ln in raw.splitlines()]
            paragraphs: list[list[str]] = []
            cur: list[str] = []
            for ln in lines:
                if ln == "":
                    if cur:
                        paragraphs.append(cur)
                        cur = []
                    # skip consecutive blanks; treat any number as a single separator
                else:
                    cur.append(re.sub(r"\s+", " ", ln))
            if cur:
                paragraphs.append(cur)
            out_parts: list[str] = []
            for p in paragraphs:
                if not p:
                    # represent paragraph break
                    out_parts.append("")
                else:
                    out_parts.append(" ".join(p))
            # collapse consecutive empty markers into single blank lines
            # then join with double-newline
            cleaned_parts: list[str] = []
            prev_empty = False
            for part in out_parts:
                if part == "":
                    if not prev_empty:
                        cleaned_parts.append("")
                        prev_empty = True
                else:
                    cleaned_parts.append(part)
                    prev_empty = False
            return "\n\n".join(cleaned_parts).strip()
        except Exception:
            return raw.strip()

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

        with contextlib.suppress(Exception):
            output.capture_internal(f"TOOL_CALL {name} {_sanitize_text(summary)}")

        # Claude Code style: ⏺ ActionVerb path/to/file
        action, _, detail = summary.partition(" ")
        if detail:
            console.print(
                f"\n  [{TOOL_COLOR}]{BLACK_CIRCLE}[/] "
                f"[bold]{markup_escape(action)}[/] "
                f"[dim]{markup_escape(detail)}[/]",
            )
        else:
            console.print(
                f"\n  [{TOOL_COLOR}]{BLACK_CIRCLE}[/] "
                f"[bold]{markup_escape(summary)}[/]",
            )

        # Update toolbar status
        if self._ss is not None:
            try:
                if hasattr(self._ss, "update"):
                    self._ss.update(
                        {"active": True, "text": f"running {summary}...", "preview": ""}
                    )
                else:
                    self._ss.active = True  # type: ignore[attr-defined]
                    self._ss.text = f"running {summary}..."  # type: ignore[attr-defined]
                    self._ss.preview = ""  # type: ignore[attr-defined]
            except Exception:
                pass

    def _show_tool_result(self, event: AgentEvent) -> None:
        raw = event.tool_result or ""
        is_err = event.is_error

        if is_err:
            err_text = _sanitize_text(raw[:300]).replace("\n", " ")
            if len(err_text) > 120:
                err_text = err_text[:117] + "..."
            console.print(
                f"    [{ERROR_COLOR}]{CROSS_MARK} {markup_escape(err_text)}[/]"
            )
            return

        # Compact success: show a snippet of the result
        snippet = _sanitize_text(raw[:200]).replace("\n", " ").strip()
        if len(snippet) > 100:
            snippet = snippet[:97] + "..."
        if snippet:
            console.print(f"    [dim]{markup_escape(snippet)}[/]")

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
                        ),
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
            action, _, detail = summary.partition(" ")
            if detail:
                console.print(
                    f"\n  [{TOOL_COLOR}]{BLACK_CIRCLE}[/] "
                    f"[bold]{markup_escape(action)}[/] "
                    f"[dim]{markup_escape(detail)}[/]",
                )
            else:
                console.print(
                    f"\n  [{TOOL_COLOR}]{BLACK_CIRCLE}[/] "
                    f"[bold]{markup_escape(summary)}[/]",
                )
        case AgentEventKind.TOOL_RESULT:
            raw = (event.tool_result or "")[:200]
            snippet = _sanitize_text(raw).replace("\n", " ").strip()
            if len(snippet) > 100:
                snippet = snippet[:97] + "..."
            if event.is_error:
                console.print(
                    f"    [{ERROR_COLOR}]{CROSS_MARK} {markup_escape(snippet)}[/]"
                )
            elif snippet:
                console.print(f"    [dim]{markup_escape(snippet)}[/]")
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
        f"{plan.pending_count} pending[/]",
    )
    if plan.all_decided:
        console.print(f"[{OK_COLOR}]All steps decided. /mode code to execute.[/]")
    else:
        console.print("[dim]/approve <n|all>  /reject <n|all>[/]")


def render_diff_summary(changes: list[Any]) -> None:
    """Render a summary of file changes with hunk details and syntax highlighting."""
    if not changes:
        print_info("No file changes in this session.")
        return
    from obscura.cli.app.diff_engine import DiffEngine

    engine = DiffEngine()

    # File-level summary header.
    total_insertions = 0
    total_deletions = 0
    total_hunks = 0
    for fc in changes:
        diff_fc = engine.compute_change(fc["path"], fc["original"], fc["modified"])
        for hunk in diff_fc.hunks:
            for ln in hunk.lines:
                if ln.tag == "+":
                    total_insertions += 1
                elif ln.tag == "-":
                    total_deletions += 1
        total_hunks += len(diff_fc.hunks)

    console.print(
        f"\n[bold]{len(changes)} file(s) changed[/]  "
        f"[{OK_COLOR}]+{total_insertions}[/]  "
        f"[{ERROR_COLOR}]-{total_deletions}[/]  "
        f"[dim]({total_hunks} hunks)[/]",
    )
    console.print(Rule(style="dim"))

    hunk_idx = 0
    for fc in changes:
        diff_fc = engine.compute_change(fc["path"], fc["original"], fc["modified"])

        # Per-file stats.
        file_ins = sum(1 for h in diff_fc.hunks for ln in h.lines if ln.tag == "+")
        file_del = sum(1 for h in diff_fc.hunks for ln in h.lines if ln.tag == "-")

        console.print(
            f"\n[bold {ACCENT}]{fc['path']}[/]  "
            f"[{OK_COLOR}]+{file_ins}[/] [{ERROR_COLOR}]-{file_del}[/]",
        )

        # Syntax-highlighted unified diff.
        unified = engine.format_unified(diff_fc)
        if unified.strip():
            try:
                console.print(
                    Syntax(unified, "diff", theme=CODE_THEME, line_numbers=True),
                )
            except Exception:
                console.print(unified)

        # Hunk status indicators.
        for hunk in diff_fc.hunks:
            status_map = {
                "accepted": f"[{OK_COLOR}]✓ accepted[/]",
                "rejected": f"[{ERROR_COLOR}]✗ rejected[/]",
                "pending": "[dim]○ pending[/]",
            }
            status = status_map.get(hunk.status, "[dim]○ pending[/]")
            console.print(
                f"  [dim]#{hunk_idx}[/] "
                f"@@ -{hunk.old_start},{hunk.old_count} "
                f"+{hunk.new_start},{hunk.new_count} @@  "
                f"{status}",
            )
            hunk_idx += 1

    console.print()
    console.print(
        "[dim]Commands: /diff overlay  /diff accept <n|all>  "
        "/diff reject <n|all>  /diff apply[/]",
    )


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
        ),
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
        console.print(
            f"  [bold {ACCENT}]{markup_escape(_sanitize_text(output_ev.agent_name))}:[/] {safe_text}",
        )
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
    BOLD = "\033[1m"
    GREEN = "\033[38;5;28m"  # Irish green
    BLUE = "\033[38;5;17m"  # deep dark navy blue
    GRAY = "\033[38;5;240m"
    CAT_BLK = "\033[38;5;232m"  # near-black for cat body
    CAT_GRY = "\033[38;5;236m"  # dark gray shading
    CAT_NOSE = "\033[38;5;175m"  # pink nose
    CAT_WHSK = "\033[38;5;250m"  # light gray whiskers
    CAT_EYE = "\033[38;5;46m"  # bright green eyes

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

    g = " " * 36  # gap between the two cats
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
        c = solid_color or (GREEN if i % 2 == 0 else BLUE)
        sys.stdout.write(f"  {BOLD}{c}{line}{RESET}\n")
    sys.stdout.write(f"\n{GRAY}  {chr(0x2501) * 66}{RESET}\n")
    sys.stdout.flush()


def _banner_obscura_by_overhaul() -> None:
    """Print OBSCURA (cyan/teal oscillating) then 'by OVERHAUL' (green/blue)."""
    import sys

    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[38;5;51m"
    TEAL = "\033[38;5;37m"
    GREEN = "\033[38;5;35m"
    GRAY = "\033[38;5;240m"

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
    agent_infos: list[Any] | None = None,
    health_checks: list[Any] | None = None,
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

    console.print(
        f"  [bold]model:[/]     [{ACCENT}]{model or 'default'}[/]   [dim]/model to change[/]",
    )
    console.print(f"  [bold]backend:[/]   [{ACCENT}]{label}[/]")
    if info_line:
        console.print(f"  {info_line}")

    # Show health warnings for degraded/unavailable optional dependencies
    if health_checks:
        console.print()
        for hc in health_checks:
            status = getattr(hc, "status", "degraded")
            if status == "unavailable":
                console.print(f"  [{ERROR_COLOR}]x {markup_escape(hc.message)}[/]")
            else:
                console.print(f"  [{WARN_COLOR}]! {markup_escape(hc.message)}[/]")

    if agent_infos:
        console.print()
        console.print(f"  [bold]Fleet agents ({len(agent_infos)}):[/]")
        for ai in agent_infos:
            type_color = {"loop": ACCENT, "daemon": "yellow", "aper": "magenta"}.get(
                getattr(ai, "type", "loop"),
                ACCENT,
            )
            status = getattr(ai, "status", "configured")
            status_icon = {"running": "●", "configured": "○", "stopped": "◌"}.get(
                status,
                "○",
            )
            status_color = {
                "running": OK_COLOR,
                "configured": "dim",
                "stopped": ERROR_COLOR,
            }.get(status, "dim")
            console.print(
                f"    [{status_color}]{status_icon}[/] "
                f"[bold]{ai.name}[/]  "
                f"[{type_color}]{ai.type}[/]  "
                f"[dim]{ai.model}[/]",
            )
        console.print(
            "  [dim]@name <prompt> to invoke, /agent spawn <name> to start[/]",
        )
    elif available_agents:
        console.print(
            f"  [bold]agents:[/]    [{ACCENT}]{', '.join(available_agents)}[/]   "
            "[dim]/agent spawn <name> or @name <prompt>[/]",
        )
    console.print()
    console.print(
        "  [dim]Type [bold]/help[/bold] for commands, [bold]/quit[/bold] to exit.[/]",
    )
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
            lines.append("> Assistant (rendered)\n")
            assistant_block = text.rstrip()
            lines.append(assistant_block)
            lines.append("")
    return "\n".join(lines)


# Model-space delta for prompt HUD tests
_model_space_delta: str = ""


def set_model_space_delta(delta: str) -> None:
    """Set a small short-lived model text delta used by prompt HUD tests."""
    global _model_space_delta
    _model_space_delta = delta


def get_model_space_delta() -> str:
    return _model_space_delta
