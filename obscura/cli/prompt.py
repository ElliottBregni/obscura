"""obscura.cli.prompt — prompt_toolkit-based input for the REPL.

Provides a modern, responsive input experience with auto-suggestions,
slash-command completion, multiline support, bordered input, and
concurrent input during streaming.
"""

from __future__ import annotations

import asyncio
import contextlib
import html as _html
import os
import random
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast, override

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.processors import (
    Processor,
    Transformation,
    TransformationInput,
)
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from obscura.core.paths import resolve_obscura_home

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from prompt_toolkit.document import Document

# ---------------------------------------------------------------------------
# Keyword highlighter — colors trigger words as you type
# ---------------------------------------------------------------------------

# Gradient palette for "ultrathink" (purple → blue → cyan)
_GRADIENT = [
    "#8b5cf6",
    "#7c3aed",
    "#6d28d9",
    "#5b21b6",
    "#4f46e5",
    "#4338ca",
    "#3b82f6",
    "#2563eb",
    "#0ea5e9",
    "#06b6d4",
]

# Keywords that trigger gradient styling, with their style names
_HIGHLIGHT_KEYWORDS: dict[str, str] = {
    "ultrathink": "keyword.ultrathink",
    "deep think": "keyword.deepthink",
    "think hard": "keyword.deepthink",
}


class KeywordHighlighter(Processor):
    """Highlight trigger words in the input with gradient colors.

    Scans the input buffer for keywords like ``ultrathink`` and applies
    per-character style classes that map to gradient colors in the style
    dict.  This makes the keyword glow as you type it.
    """

    @override
    def apply_transformation(
        self,
        transformation_input: TransformationInput,
    ) -> Transformation:
        fragments = transformation_input.fragments
        doc = transformation_input.document
        text = doc.text_before_cursor + doc.text_after_cursor

        if not text:
            return Transformation(fragments)

        # Flatten the existing fragments into a single string to match positions.
        # OneStyleAndTextTuple is (style, text) or (style, text, mouse_handler);
        # index [1] always gives the text part.
        flat = "".join(frag[1] for frag in fragments)
        if not flat:
            return Transformation(fragments)

        # Build a set of character positions that should be styled
        styled_positions: dict[int, str] = {}
        for keyword, _style_name in _HIGHLIGHT_KEYWORDS.items():
            start = 0
            kw_lower = keyword.lower()
            flat_lower = flat.lower()
            while True:
                idx = flat_lower.find(kw_lower, start)
                if idx == -1:
                    break
                for i in range(len(keyword)):
                    # Map each char to a gradient color index
                    gradient_idx = i % len(_GRADIENT)
                    styled_positions[idx + i] = f"class:kw-g{gradient_idx}"
                start = idx + 1

        if not styled_positions:
            return Transformation(fragments)

        # Rebuild fragments with styled characters
        new_fragments: list[tuple[str, str]] = []
        pos = 0
        for frag in fragments:
            style = frag[0]
            segment = frag[1]
            for ch in segment:
                if pos in styled_positions:
                    new_fragments.append((styled_positions[pos], ch))
                else:
                    new_fragments.append((style, ch))
                pos += 1

        # cast: new_fragments is list[tuple[str, str]] which is a valid
        # specialization of StyleAndTextTuples (list[OneStyleAndTextTuple])
        # but pyright doesn't see the OneStyleAndTextTuple union covariantly.
        return Transformation(cast(Any, new_fragments))


def _keyword_gradient_styles() -> dict[str, str]:
    """Build style entries for the gradient character classes."""
    styles: dict[str, str] = {}
    for i, color in enumerate(_GRADIENT):
        styles[f"kw-g{i}"] = f"{color} bold"
    return styles


# ---------------------------------------------------------------------------
# StreamingStatus — shared mutable state for toolbar spinner
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_THINKING_MESSAGES = [
    "thinking...",
    "pondering...",
    "mulling it over...",
    "ruminating...",
    "contemplating...",
    "brewing ideas...",
    "connecting dots...",
    "noodling on it...",
    "chewing on that...",
    "working through it...",
    "processing...",
    "deep in thought...",
    "considering options...",
    "assembling thoughts...",
    "piecing it together...",
]


def random_thinking_message() -> str:
    """Return a random thinking status message."""
    return random.choice(_THINKING_MESSAGES)


@dataclass
class StreamingStatus:
    """Mutable bag updated by StreamRenderer, read by the toolbar callable."""

    active: bool = False
    text: str = ""  # e.g. "thinking...", "running edit_file..."
    preview: str = ""  # thinking-delta preview snippet
    spinner_idx: int = 0

    @property
    def spinner_char(self) -> str:
        return _SPINNER_FRAMES[self.spinner_idx % len(_SPINNER_FRAMES)]

    def reset(self) -> None:
        self.active = False
        self.text = ""
        self.preview = ""


async def animate_spinner(status: StreamingStatus) -> None:
    """Background task: advance spinner frame + invalidate prompt toolbar."""
    from prompt_toolkit.application import get_app_or_none

    while True:
        await asyncio.sleep(0.1)
        if not status.active:
            continue
        status.spinner_idx = (status.spinner_idx + 1) % len(_SPINNER_FRAMES)
        try:
            app = get_app_or_none()
            if app is not None:
                app.invalidate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# PromptStatus — live state shown in the banner above the input box
# ---------------------------------------------------------------------------


@dataclass
class RunningAgentInfo:
    """Snapshot of a single running agent for display."""

    name: str
    status: str = "running"  # running | waiting | pending
    elapsed_s: float = 0.0
    iteration_count: int = 0
    last_tool: str = ""

    @property
    def elapsed_display(self) -> str:
        s = int(self.elapsed_s)
        if s < 60:
            return f"{s}s"
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s"


@dataclass
class PromptStatus:
    """Mutable state bag read by print_status_banner() on every prompt call."""

    model: str = ""
    branch: str = ""
    ctx_pct: int = 0  # 0-100
    ctx_tokens: int = 0
    ctx_window: int = 0
    mode: str = ""
    session_id: str = ""
    session_title: str = ""  # auto-generated or manual title
    running_agents: list[str] = field(default_factory=list[str])
    agent_details: list[RunningAgentInfo] = field(
        default_factory=list[RunningAgentInfo]
    )
    task_count: int = 0


def _get_git_branch() -> str:  # pyright: ignore[reportUnusedFunction]
    """Return the current git branch name, or '' if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch and branch != "HEAD" else ""
    except Exception:
        pass
    return ""


def print_status_banner(status: PromptStatus) -> None:
    """Print a Claude Code-style status line above the input box.

    Format:
      Session Title (abc12345)
      claude-opus-4 · 12.3k tokens (42%) · ⎇ main · code

    Uses Rich markup via the shared console.
    """
    from rich.markup import escape as markup_escape

    from obscura.cli.render import console

    # Line 1: Session title (if available)
    if status.session_id:
        short_id = status.session_id[:8]
        if status.session_title:
            console.print(
                f"  [bold {_C_TEXT.hex}]{markup_escape(status.session_title)}[/]"
                f"  [dim]({short_id})[/]",
                highlight=False,
            )

    # Line 2: model · tokens · branch · mode
    parts: list[str] = []

    if status.model:
        parts.append(f"[bold {_C_BLUE.hex}]{markup_escape(status.model)}[/]")

    if status.ctx_pct > 0 or status.ctx_tokens > 0:
        pct = status.ctx_pct
        if pct >= 80:
            color = f"bold {_C_RED.hex}"
        elif pct >= 60:
            color = _C_PEACH.hex
        else:
            color = _C_GREEN.hex
        if status.ctx_tokens:
            t = status.ctx_tokens
            if t >= 1000:
                ctx_str = f"{t / 1000:.1f}k tokens ({pct}%)"
            else:
                ctx_str = f"{t} tokens ({pct}%)"
        else:
            ctx_str = f"ctx {pct}%"
        parts.append(f"[{color}]{ctx_str}[/]")

    if status.branch:
        parts.append(f"[{_C_TEAL.hex}]⎇ {markup_escape(status.branch)}[/]")

    if status.mode:
        parts.append(f"[dim]{markup_escape(status.mode)} mode[/]")

    if parts:
        sep = "  [dim]·[/]  "
        console.print(f"  {sep.join(parts)}", highlight=False)

    # Running agents line
    if status.running_agents:
        agent_labels = [
            f"[bold {_C_GREEN.hex}]{markup_escape(name)}[/] [{_C_GREEN.hex}]●[/]"
            for name in status.running_agents
        ]
        console.print(
            f"  [dim]agents:[/] {' '.join(agent_labels)}",
            highlight=False,
        )


# ---------------------------------------------------------------------------
# Slash-command completer
# ---------------------------------------------------------------------------


class SlashCommandCompleter(Completer):
    """Tab-complete /commands, @commands, and $skills (including chains)."""

    def __init__(
        self,
        completions: dict[str, list[str]],
        at_command_names: Callable[[], list[str]] | None = None,
        dollar_skill_names: Callable[[], list[str]] | None = None,
    ) -> None:
        self._completions = completions
        self._at_command_names = at_command_names
        self._dollar_skill_names = dollar_skill_names

    @override
    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        text = document.text_before_cursor.lstrip()

        # /slash commands — only at the very start
        if text.startswith("/"):
            parts = text.split()
            if len(parts) <= 1:
                prefix = text[1:]
                for cmd in sorted(self._completions):
                    if cmd.startswith(prefix):
                        yield Completion(
                            "/" + cmd,
                            start_position=-len(text),
                            display="/" + cmd,
                        )
                return
            cmd = parts[0].lstrip("/")
            subs = self._completions.get(cmd, [])
            if not subs:
                return
            partial = parts[1] if len(parts) > 1 else ""
            for sub in sorted(subs):
                if sub.startswith(partial):
                    yield Completion(sub, start_position=-len(partial))
            return

        # $ and @ — complete the current (last) token in a chain
        # e.g. "$python @rev" -> complete "@review"
        # e.g. "$py" -> complete "$python"
        # e.g. "$python $se" -> complete "$security"
        word = document.get_word_before_cursor(WORD=True)
        if not word:
            return

        if word.startswith("$") and self._dollar_skill_names is not None:
            prefix = word[1:]
            for name in self._dollar_skill_names():
                if name.startswith(prefix):
                    yield Completion(
                        "$" + name,
                        start_position=-len(word),
                        display="$" + name,
                    )
            return

        if word.startswith("@") and self._at_command_names is not None:
            prefix = word[1:]
            for name in self._at_command_names():
                if name.startswith(prefix):
                    yield Completion(
                        "@" + name,
                        start_position=-len(word),
                        display="@" + name,
                    )
            return


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

# Import Catppuccin Mocha hex values from the single source of truth.
from obscura.cli.renderer.modern.theme import (
    BLUE as _C_BLUE,
    GREEN as _C_GREEN,
    LAVENDER as _C_LAVENDER,
    OVERLAY0 as _C_OVERLAY0,
    PEACH as _C_PEACH,
    RED as _C_RED,
    SUBTEXT0 as _C_SUBTEXT0,
    SURFACE1 as _C_SURFACE1,
    TEAL as _C_TEAL,
    TEXT as _C_TEXT,
)

PROMPT_STYLE = Style.from_dict(
    {
        "prompt": f"{_C_LAVENDER.hex} bold",
        "prompt-border": _C_SURFACE1.hex,
        "prompt-border-accent": _C_BLUE.hex,
        "status-line": _C_OVERLAY0.hex,
        "status-spinner": f"bold {_C_BLUE.hex}",
        "status-preview": f"italic {_C_OVERLAY0.hex}",
        "continuation": _C_OVERLAY0.hex,
        "bottom-toolbar": f"{_C_SUBTEXT0.hex} noreverse",
        "bottom-toolbar.key": f"bold {_C_BLUE.hex}",
        # Keyword gradient colors (used by KeywordHighlighter)
        **_keyword_gradient_styles(),
    },
)


def _make_prompt_message() -> HTML:  # pyright: ignore[reportUnusedFunction]
    return HTML("<prompt>\u276f </prompt>")


# ---------------------------------------------------------------------------
# Separator
# ---------------------------------------------------------------------------

_RULE_CHAR = "\u2500"  # ─


def print_separator() -> None:
    """Print a subtle separator between turns."""
    import sys as _sys

    _sys.stdout.write("\n")
    _sys.stdout.flush()


def print_turn_separator() -> None:
    """Print a thin visual break between turns — just breathing room."""
    import sys as _sys

    _sys.stdout.write("\n")
    _sys.stdout.flush()


# ---------------------------------------------------------------------------
# Key bindings
# ---------------------------------------------------------------------------


def _expand_preview_action() -> None:
    """Print the full accumulated assistant text from the active renderer."""
    try:
        from rich.markdown import Markdown

        from obscura.cli.render import console, get_active_text

        text = get_active_text()
        if not text:
            console.print("[dim]No preview available to expand.[/]")
            return
        console.print()
        console.print(Markdown(text))
        console.print()
    except Exception:
        pass


def _make_key_bindings(expand_key: str = "c-p") -> KeyBindings:
    """Enter submits, Escape+Enter inserts newline for multiline.

    expand_key may be a prompt_toolkit key spec (default Ctrl-P -> 'c-p').
    """
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _insert_newline(event: object) -> None:  # pyright: ignore[reportUnusedFunction]
        from prompt_toolkit.key_binding import KeyPressEvent

        assert isinstance(event, KeyPressEvent)
        event.current_buffer.insert_text("\n")

    # Expand preview hotkey
    try:

        @kb.add(expand_key)
        def _expand(event: object) -> None:  # pyright: ignore[reportUnusedFunction]
            _expand_preview_action()
    except Exception:
        # ignore invalid key spec
        pass

    # Expand last thinking block
    @kb.add("c-t")
    def _expand_thinking(event: object) -> None:  # pyright: ignore[reportUnusedFunction]
        _expand_thinking_action()

    # Voice input: Ctrl+Space triggers push-to-talk recording
    @kb.add("c-space")
    def _voice_record(event: object) -> None:  # pyright: ignore[reportUnusedFunction]
        from prompt_toolkit.key_binding import KeyPressEvent

        assert isinstance(event, KeyPressEvent)
        buf = event.current_buffer
        # Insert a voice marker that the REPL will intercept.
        buf.text = "__VOICE_RECORD__"
        buf.validate_and_handle()

    return kb


def _expand_thinking_action() -> None:
    """Print the last thinking block from the active renderer."""
    try:
        from rich.panel import Panel
        from rich.text import Text

        from obscura.cli.render import (
            THINKING_COLOR,
            _active_renderer,  # pyright: ignore[reportPrivateUsage]
            console,
        )

        if _active_renderer is None:
            console.print("[dim]No active session.[/]")
            return
        last = _active_renderer.get_last_thinking()
        if not last:
            console.print("[dim]No thinking blocks available.[/]")
            return
        console.print()
        console.print(
            Panel(
                Text(last, style="dim italic"),
                title=f"[{THINKING_COLOR}]reasoning (expanded)[/]",
                title_align="left",
                border_style="dim magenta",
                expand=False,
                padding=(0, 1),
            ),
        )
        console.print()
    except Exception:
        pass


# Public helper for tests to call expand action
expand_preview = _expand_preview_action
expand_thinking = _expand_thinking_action


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


def _build_toolbar_html(prompt_status: PromptStatus | None) -> str:
    """Build the bottom toolbar: status line + optional agent panel.

    Line 1: session · model · context % · hints
    Line 2+: running agent tree (only when agents are active)
    """
    if prompt_status is None:
        return "  <b>!</b> for bash · <b>/help</b> for commands · <b>esc+enter</b> for newline"

    left_parts: list[str] = []
    right_parts: list[str] = []

    # Session title or short ID
    if prompt_status.session_title:
        left_parts.append(f"<b>{_html.escape(prompt_status.session_title)}</b>")
    elif prompt_status.session_id:
        left_parts.append(
            f"<style fg='{_C_OVERLAY0.hex}'>"
            f"{_html.escape(prompt_status.session_id[:8])}</style>"
        )

    # Model
    if prompt_status.model:
        left_parts.append(_html.escape(prompt_status.model))

    # Context usage
    if prompt_status.ctx_pct > 0:
        pct = prompt_status.ctx_pct
        if pct >= 80:
            ctx_str = f"<style fg='{_C_RED.hex}'>{pct}% context</style>"
        elif pct >= 60:
            ctx_str = f"<style fg='{_C_PEACH.hex}'>{pct}% context</style>"
        else:
            ctx_str = f"{pct}% context"
        left_parts.append(ctx_str)

    # Task count if active
    if prompt_status.task_count > 0:
        left_parts.append(f"<b>{prompt_status.task_count}</b> tasks")

    # Agent count
    if prompt_status.running_agents:
        n = len(prompt_status.running_agents)
        left_parts.append(f"<b>{n}</b> agent{'s' if n != 1 else ''}")

    # Shortcut hints (right side)
    right_parts.append("<b>!</b> bash")
    right_parts.append("<b>/help</b>")
    right_parts.append("<b>esc+enter</b> newline")

    left = " · ".join(left_parts)
    right = " · ".join(right_parts)
    status_line = f"  {left}    {right}"

    # ── Agent panel (tree-connected lines below status) ─────────────────
    agents = prompt_status.agent_details
    if not agents:
        return status_line

    lines = [status_line]
    for i, ag in enumerate(agents):
        is_last = i == len(agents) - 1
        tree = "└─" if is_last else "├─"

        # Status indicator
        if ag.status == "running":
            bullet = "<style fg='#a6e3a1'>●</style>"
        elif ag.status == "waiting":
            bullet = "<style fg='#fab387'>○</style>"
        else:
            bullet = "<style fg='#6c7086'>◌</style>"

        name_esc = _html.escape(ag.name)
        elapsed = _html.escape(ag.elapsed_display)

        agent_line = (
            f"  <style fg='#45475a'>{tree}</style> "
            f"{bullet} <b>{name_esc}</b>"
            f"  <style fg='#6c7086'>{elapsed}</style>"
        )

        if ag.iteration_count > 0:
            agent_line += f"  <style fg='#6c7086'>{ag.iteration_count} turns</style>"

        lines.append(agent_line)

        # Last tool activity line
        if ag.last_tool and ag.status == "running":
            pad = "   " if is_last else "<style fg='#45475a'>│</style>  "
            tool_esc = _html.escape(ag.last_tool)
            lines.append(f"  {pad}<style fg='#89b4fa'>⍿ {tool_esc}</style>")

    return "\n".join(lines)


def create_prompt_session(
    completions: dict[str, list[str]],
    toolbar_text: str = "",
    streaming_status: StreamingStatus | None = None,
    prompt_status: PromptStatus | None = None,
    at_command_names: Callable[[], list[str]] | None = None,
    dollar_skill_names: Callable[[], list[str]] | None = None,
    hud_provider: Callable[[], dict[str, Any]] | None = None,
) -> PromptSession[str]:
    """Create a configured PromptSession for the Obscura REPL."""
    # Ensure the Obscura home directory exists so FileHistory can write.
    home = resolve_obscura_home()
    with contextlib.suppress(Exception):
        home.mkdir(parents=True, exist_ok=True)
    history_path = home / "cli_history_v2"

    _fallback_text = f"  {toolbar_text}" if toolbar_text else ""
    _status = streaming_status
    _prompt_status = prompt_status

    def _message() -> HTML:
        # When streaming, dim the prompt character
        if _status is not None and _status.active:
            return HTML("<status-line>\u276f </status-line>")

        # Idle: clean ❯ prompt — no borders, no decoration
        return HTML("<prompt>\u276f </prompt>")

    # If a static hud_provider was supplied, compute a one-shot toolbar
    _static_hud_html: str | None = None
    if hud_provider is not None:
        try:
            data: dict[str, Any] = hud_provider() or {}
            menu_raw: Any = data.get("menu_items", [])
            menu: list[tuple[str, str]] = []
            if isinstance(menu_raw, list):
                for item_any in cast(list[Any], menu_raw):
                    if isinstance(item_any, (list, tuple)):
                        item_seq = cast("list[Any] | tuple[Any, ...]", item_any)
                        if len(item_seq) >= 2:
                            menu.append((str(item_seq[0]), str(item_seq[1])))
            tasks = ""
            for k, v in menu:
                if k == "tasks":
                    tasks = v
            approvals_on = any(k == "approvals" and v == "on" for k, v in menu)
            reasoning_on = any(k == "reasoning" and v == "on" for k, v in menu)
            model_text = ""
            if data.get("model_enabled"):
                try:
                    from obscura.cli.render import get_model_space_delta

                    model_text = get_model_space_delta()
                except Exception:
                    model_text = ""
            hud = PromptHUDState(
                model_text=model_text,
                right_enabled=bool(data.get("right_enabled", False)),
                tasks_value=tasks,
                approvals_enabled=approvals_on,
                reasoning_enabled=reasoning_on,
                menu_items=menu,
            )
            _static_hud_html = _render_menu_line(80, hud, PromptLayoutConfig())
        except Exception:
            _static_hud_html = None

    def _toolbar() -> HTML:
        if _static_hud_html is not None:
            return HTML(_static_hud_html)
        if _prompt_status is not None:
            return HTML(_build_toolbar_html(_prompt_status))
        return HTML(_fallback_text)

    session: PromptSession[str] = PromptSession(
        message=_message,
        style=PROMPT_STYLE,
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=SlashCommandCompleter(
            completions,
            at_command_names=at_command_names,
            dollar_skill_names=dollar_skill_names,
        ),
        complete_while_typing=False,
        key_bindings=_make_key_bindings(
            os.environ.get("OBSCURA_EXPAND_PREVIEW_KEY", "c-p"),
        ),
        input_processors=[KeywordHighlighter()],
        enable_history_search=True,
        mouse_support=False,
        prompt_continuation="  \u00b7 ",
        bottom_toolbar=_toolbar,
    )
    return session


# ---------------------------------------------------------------------------
# Bordered prompt (separator + prompt + separator)
# ---------------------------------------------------------------------------


async def bordered_prompt(
    session: PromptSession[str],
    status: PromptStatus | None = None,  # kept for call-site compat
) -> str:
    """Prompt for user input.

    When Textual TUI is active, awaits the Textual Input widget queue.
    Otherwise uses prompt_toolkit with the ❯ prompt.
    """
    with patch_stdout(raw=True):
        result = await session.prompt_async()
    return result.strip()


# ---------------------------------------------------------------------------
# Confirm prompt (async one-shot)
# ---------------------------------------------------------------------------


async def confirm_prompt_async(message: str = "Allow? [y/n/always] ") -> str:
    """Async one-shot prompt for tool confirmation."""
    session: PromptSession[str] = PromptSession()
    try:
        # Wrap with patch_stdout to avoid interleaved prints when other tasks log.
        with patch_stdout(raw=True):
            return (await session.prompt_async(message)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "n"


@dataclass
class PromptLayoutConfig:
    model_hpad: int = 2
    input_hpad: int = 2
    model_vpad: int = 0
    input_vpad: int = 0
    menu_hpad: int = 1


@dataclass
class PromptHUDState:
    model_text: str = ""
    right_enabled: bool = False
    tasks_value: str = ""
    approvals_enabled: bool = False
    reasoning_enabled: bool = False
    menu_items: list[tuple[str, str]] = field(
        default_factory=lambda: cast(list[tuple[str, str]], [])
    )


def _build_prompt_message_html(
    width: int,
    model_text: str,
    cfg: PromptLayoutConfig,
) -> str:
    status = f"<status-lane>{model_text}</status-lane>"
    input_lane = "<input-lane>\u2502 </input-lane>"
    return status + "\n" + input_lane


def _render_model_status_line(width: int, hud: PromptHUDState) -> str:
    parts: list[str] = []
    if hud.tasks_value:
        parts.append(f"T:{hud.tasks_value}")
    parts.append("A:on" if hud.approvals_enabled else "A:off")
    parts.append("R:on" if hud.reasoning_enabled else "R:off")
    left = hud.model_text or ""
    line = (left + " " + " ".join(parts)).strip()
    if len(line) > width:
        return line[:width]
    return line


def _render_menu_line(width: int, hud: PromptHUDState, cfg: PromptLayoutConfig) -> str:
    # Render menu items compactly with menu_hpad spacing
    items = hud.menu_items or []
    menu = " ".join(f"{k}:{v}" for k, v in items)
    base = _render_model_status_line(width, hud)
    line = f"{base} {menu}".strip()
    if len(line) > width:
        return line[:width]
    return line


# expose aliases expected by tests
_build_prompt_message_html = _build_prompt_message_html
_render_model_status_line = _render_model_status_line
_render_menu_line = _render_menu_line
