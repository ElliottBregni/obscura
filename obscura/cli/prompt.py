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
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from obscura.core.paths import resolve_obscura_home

if TYPE_CHECKING:
    from collections.abc import Callable

    from prompt_toolkit.document import Document

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
class PromptStatus:
    """Mutable state bag read by print_status_banner() on every prompt call."""

    model: str = ""
    branch: str = ""
    ctx_pct: int = 0  # 0-100
    ctx_tokens: int = 0
    ctx_window: int = 0
    mode: str = ""
    session_id: str = ""
    running_agents: list[str] = field(default_factory=list[str])
    task_count: int = 0


def _get_git_branch() -> str:
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
    """Print a session banner above the input separator.

    Line 1:  session abc12def  ·  ⎇ main  ·  claude-opus-4  ·  ctx 42%  ·  code
    Line 2:  agents: researcher ● health-monitor ●   (only when agents are running)

    Uses Rich markup via the shared console.
    """
    from rich.markup import escape as markup_escape

    from obscura.cli.render import ACCENT, console

    parts: list[str] = []

    if status.session_id:
        short_id = status.session_id[:8]
        parts.append(f"[bold {ACCENT}]session {markup_escape(short_id)}[/]")

    if status.branch:
        parts.append(f"[bold cyan]⎇ {markup_escape(status.branch)}[/]")

    if status.model:
        parts.append(f"[dim]{markup_escape(status.model)}[/]")

    if status.ctx_pct > 0 or status.ctx_tokens > 0:
        pct = status.ctx_pct
        if pct >= 80:
            color = "bold red"
        elif pct >= 60:
            color = "yellow"
        else:
            color = "dim green"
        ctx_str = f"ctx {pct}%"
        if status.ctx_tokens:
            ctx_str += f" ({status.ctx_tokens:,})"
        parts.append(f"[{color}]{ctx_str}[/]")

    if status.mode:
        parts.append(f"[dim]mode: {markup_escape(status.mode)}[/]")

    if not parts:
        return

    sep = "  [dim]·[/]  "
    line = sep.join(parts)
    console.print(f"  {line}", highlight=False)

    # Running agents line
    if status.running_agents:
        agent_labels = [
            f"[bold green]{markup_escape(name)}[/] [green]●[/]"
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
    ) -> list[Completion]:
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
            return []

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

        return []


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "#6c71c4 bold",
        "status-line": "#586e75",
        "status-spinner": "bold #6c71c4",
        "status-preview": "italic #586e75",
        "continuation": "#586e75",
        "bottom-toolbar": "#00cc00 noreverse",
    },
)


def _make_prompt_message() -> HTML:
    return HTML("<prompt>\u276f </prompt>")


# ---------------------------------------------------------------------------
# Separator
# ---------------------------------------------------------------------------

_RULE_CHAR = "\u2500"  # ─


def print_separator() -> None:
    """Print a thin horizontal rule across the terminal width."""
    from obscura.cli.render import console

    width = shutil.get_terminal_size((80, 24)).columns
    console.print(f"[dim]{_RULE_CHAR * width}[/]", highlight=False)


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

        from obscura.cli.render import THINKING_COLOR, _active_renderer, console

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
    """Build a two-line bottom toolbar from live PromptStatus.

    Line 1: session · agents · ctx
    Line 2: mode · shortcuts
    """
    if prompt_status is None:
        return "  esc+enter multiline · /help"

    # --- top row: session, agents, context ---
    top: list[str] = []

    if prompt_status.session_id:
        short_id = prompt_status.session_id[:8]
        top.append(f"session {short_id}")

    if prompt_status.running_agents:
        agents_str = " ".join(
            f"{_html.escape(n)} ●" for n in prompt_status.running_agents
        )
        top.append(agents_str)

    if prompt_status.task_count > 0:
        top.append(f"tasks: {prompt_status.task_count} ●")

    # Always show context — empty on startup, fills in as conversation grows
    pct = prompt_status.ctx_pct
    if prompt_status.ctx_tokens:
        top.append(f"context: {pct}% ({prompt_status.ctx_tokens:,})")
    else:
        top.append("context:")

    # --- bottom row: mode, shortcuts ---
    bot: list[str] = []

    if prompt_status.mode:
        bot.append(f"mode: {_html.escape(prompt_status.mode)}")

    bot.append("esc+enter multiline")
    bot.append("/help")

    top_line = "  " + " · ".join(top) if top else ""
    bot_line = "  " + " · ".join(bot)

    if top_line:
        return f"{top_line}\n{bot_line}"
    return bot_line


def create_prompt_session(
    completions: dict[str, list[str]],
    toolbar_text: str = "",
    streaming_status: StreamingStatus | None = None,
    prompt_status: PromptStatus | None = None,
    at_command_names: Callable[[], list[str]] | None = None,
    dollar_skill_names: Callable[[], list[str]] | None = None,
    hud_provider: Callable[[], dict] | None = None,
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

    # Fixed thinking delta line above ❯ — always reserved, never collapses.
    def _message() -> HTML:
        # Produce a two-line HTML fragment with <status-lane> and <input-lane>
        width = shutil.get_terminal_size((80, 24)).columns
        # Active streaming status shows spinner + label in the status lane
        if _status is not None and _status.active:
            frame = _html.escape(_status.spinner_char)
            label = _html.escape(_status.text or "working...")
            preview = _status.preview
            label_part = f"<status-spinner>{frame}</status-spinner> {label}"
            if preview:
                # trim preview to available width
                max_prev = width - len(label) - 10
                if len(preview) > max_prev:
                    preview = preview[: max_prev - 3] + "..."
                label_part = (
                    label_part
                    + f" <status-preview>{_html.escape(preview)}</status-preview>"
                )
            status_lane = f"<status-lane>{label_part}</status-lane>"
            input_lane = "<input-lane>\u276f </input-lane>"
            return HTML(status_lane + "\n" + input_lane)

        # Idle: render the standard two-lane layout using model-space delta
        try:
            from obscura.cli.render import get_model_space_delta

            model_text = get_model_space_delta()
        except Exception:
            model_text = ""
        return HTML(_build_prompt_message_html(width, model_text, PromptLayoutConfig()))

    # If a static hud_provider was supplied, compute a one-shot toolbar
    _static_hud_html: str | None = None
    if hud_provider is not None:
        try:
            data = hud_provider() or {}
            menu = data.get("menu_items", [])
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
        enable_history_search=True,
        mouse_support=False,
        prompt_continuation="  \u00b7 ",
        bottom_toolbar=_toolbar,
    )
    return session


# ---------------------------------------------------------------------------
# Bordered prompt (separator + prompt + separator)
# ---------------------------------------------------------------------------


async def bordered_prompt(session: PromptSession[str]) -> str:
    """Prompt for input, then rewrite the submitted line without the ❯ prefix.

    After prompt_toolkit renders ``❯ user text``, we erase the prompt lines
    (thinking-delta + input) and reprint just the bare user text so the
    conversation history looks clean.
    """
    with patch_stdout(raw=True):
        result = await session.prompt_async()

    text = result.strip()
    if text:
        import sys

        # Erase the two lines prompt_toolkit left (thinking-delta + ❯ input)
        sys.stdout.write("\033[A\033[2K")  # up + clear (input line)
        sys.stdout.write("\033[A\033[2K")  # up + clear (thinking-delta line)
        sys.stdout.write(f"{text}\n")
        sys.stdout.flush()

    return text


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


# Backwards-compatible dataclass expected by older tests
@dataclass
class PromptLayoutConfig:
    show_session: bool = True
    show_branch: bool = True
    show_model: bool = True
    show_context: bool = True


# Compatibility helpers and HUD/layout classes used by older tests
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
    menu_items: list[tuple[str, str]] = field(default_factory=list)


def _build_prompt_message_html(
    width: int,
    model_text: str,
    cfg: PromptLayoutConfig,
) -> str:
    # Minimal two-lane message: status lane then input lane
    status = f"<status-lane>{model_text}</status-lane>"
    # input lane contains a vertical bar placeholder
    input_lane = "<input-lane>\u2502 </input-lane>"
    return status + "\n" + input_lane


def _render_model_status_line(width: int, hud: PromptHUDState) -> str:
    parts = []
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


# Compatibility helpers and HUD/layout classes used by older tests
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
    menu_items: list[tuple[str, str]] = field(default_factory=list)


def _build_prompt_message_html(
    width: int,
    model_text: str,
    cfg: PromptLayoutConfig,
) -> str:
    # Minimal two-lane message: status lane then input lane
    status = f"<status-lane>{model_text}</status-lane>"
    # input lane contains a vertical bar placeholder
    input_lane = "<input-lane>\u2502 </input-lane>"
    return status + "\n" + input_lane


def _render_model_status_line(width: int, hud: PromptHUDState) -> str:
    parts = []
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
