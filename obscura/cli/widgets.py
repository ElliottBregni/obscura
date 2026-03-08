"""obscura.cli.widgets — TUI confirmation and question widgets.

Interactive prompt_toolkit-based widgets for tool confirmations,
agent attention requests, and model questions.  Replaces the basic
``confirm_prompt_async`` text prompt with arrow-key selectable
action bars and syntax-highlighted preview panels.

Usage::

    from obscura.cli.widgets import confirm_tool, ToolConfirmRequest

    result = await confirm_tool(ToolConfirmRequest(
        tool_name="write_text_file",
        tool_input={"path": "/tmp/foo.py", "text": "print('hi')"},
    ))
    if result.action == "allow":
        ...
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from obscura.cli.render import (
    ACCENT,
    CODE_THEME,
    ERROR_COLOR,
    OK_COLOR,
    TOOL_COLOR,
    WARN_COLOR,
    _detect_language,
    _sanitize_text,
    console,
)
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

__all__ = [
    "ToolConfirmRequest",
    "AttentionWidgetRequest",
    "ModelQuestionRequest",
    "PermissionWidgetRequest",
    "NotifyWidgetRequest",
    "WidgetResult",
    "confirm_tool",
    "confirm_attention",
    "confirm_permission",
    "detect_question_choices",
    "present_detected_choices",
    "ask_model_question",
    "render_notification_banner",
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_WIDGET_STYLE = Style.from_dict(
    {
        "selected": "bold reverse #6c71c4",
        "unselected": "#586e75",
    }
)


@dataclass(frozen=True)
class WidgetResult:
    """Unified return from any confirmation/question widget."""

    action: str  # "allow", "deny", "always_allow", or custom action string
    text: str = ""  # optional free-form text (model questions)


@dataclass(frozen=True)
class ToolConfirmRequest:
    """Input for tool confirmation widget."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str = ""


@dataclass(frozen=True)
class AttentionWidgetRequest:
    """Input for agent attention request widget."""

    request_id: str
    agent_name: str
    message: str
    priority: str = "normal"  # low / normal / high / critical
    actions: tuple[str, ...] = ("ok",)
    context: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


@dataclass(frozen=True)
class ModelQuestionRequest:
    """Input for model question widget (free-form user response)."""

    question: str
    source: str = "assistant"


@dataclass(frozen=True)
class PermissionWidgetRequest:
    """Input for permission request widget."""

    action: str  # what action is being requested
    reason: str  # why it's needed
    risk: str = "low"  # low / medium / high / critical


@dataclass(frozen=True)
class NotifyWidgetRequest:
    """Input for notification banner widget."""

    title: str
    message: str
    priority: str = "normal"  # low / normal / high / critical


# ---------------------------------------------------------------------------
# Arg formatting helpers
# ---------------------------------------------------------------------------

_MAX_ARG_LINES = 10
_MAX_ARG_CHARS = 500


def _format_arg_value(val: Any) -> str:
    """Format a tool argument value for display."""
    if isinstance(val, (dict, list)):
        try:
            formatted = json.dumps(val, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            formatted = str(val)
    else:
        formatted = str(val)

    lines = formatted.split("\n")
    if len(lines) > _MAX_ARG_LINES:
        truncated = "\n".join(lines[:_MAX_ARG_LINES])
        remaining = len(formatted) - len(truncated)
        return f"{truncated}\n... ({remaining} more chars, {len(lines) - _MAX_ARG_LINES} more lines)"

    if len(formatted) > _MAX_ARG_CHARS:
        return formatted[:_MAX_ARG_CHARS] + f"... ({len(formatted)} chars total)"

    return formatted


# ---------------------------------------------------------------------------
# Rich panel renderers
# ---------------------------------------------------------------------------


def _render_tool_panel(tool_name: str, tool_input: dict[str, Any]) -> None:
    """Print a Rich panel with tool name and syntax-highlighted args."""
    if not tool_input:
        console.print(
            Panel(
                "[dim]no arguments[/]",
                title=f"[{TOOL_COLOR}]Tool: {markup_escape(tool_name)}[/]",
                title_align="left",
                border_style=TOOL_COLOR,
                expand=False,
                padding=(0, 2),
            )
        )
        return

    rows: list[str] = []
    for key, val in tool_input.items():
        val_str = _format_arg_value(val)
        val_lines = val_str.split("\n")
        safe_key = markup_escape(_sanitize_text(key))

        if len(val_lines) == 1:
            safe_val: str = markup_escape(_sanitize_text(val_lines[0]))
            rows.append(f"  [dim]{safe_key}[/] = {safe_val}")
        else:
            rows.append(f"  [dim]{safe_key}[/] =")
            # Try syntax highlighting for multi-line values
            lang = _detect_language(val_str)
            if lang:
                try:
                    syn = Syntax(
                        val_str.strip(),
                        lang,
                        theme=CODE_THEME,
                        line_numbers=False,
                        word_wrap=True,
                        padding=(0, 1),
                    )
                    console.print(
                        Panel(
                            Text(""),  # placeholder for title-only header
                            title=f"[{TOOL_COLOR}]Tool: {markup_escape(tool_name)}[/]",
                            title_align="left",
                            border_style=TOOL_COLOR,
                            expand=False,
                            padding=(0, 2),
                        )
                    )
                    # Print args with syntax block separately
                    for prev_row in rows[:-1]:
                        console.print(prev_row)
                    console.print(f"  [dim]{safe_key}[/] =")
                    console.print(syn)
                    return
                except Exception:
                    pass

            for line in val_lines:
                safe_line: str = markup_escape(_sanitize_text(line))
                rows.append(f"    {safe_line}")

    body = "\n".join(rows)
    console.print(
        Panel(
            body,
            title=f"[{TOOL_COLOR}]Tool: {markup_escape(tool_name)}[/]",
            title_align="left",
            border_style=TOOL_COLOR,
            expand=False,
            padding=(0, 2),
        )
    )


def _render_attention_panel(request: AttentionWidgetRequest) -> None:
    """Print a Rich panel for an attention request."""
    priority_border = {
        "low": "dim",
        "normal": WARN_COLOR,
        "high": f"bold {WARN_COLOR}",
        "critical": f"bold {ERROR_COLOR}",
    }
    border = priority_border.get(request.priority, WARN_COLOR)

    body_parts: list[str] = [markup_escape(_sanitize_text(request.message))]
    if request.context:
        body_parts.append("")
        for k, v in request.context.items():
            safe_k: str = markup_escape(_sanitize_text(str(k)))
            safe_v: str = markup_escape(_sanitize_text(str(v)[:120]))
            body_parts.append(f"  [dim]{safe_k}:[/] {safe_v}")

    console.print(
        Panel(
            "\n".join(body_parts),
            title=f"[{border}]{markup_escape(request.agent_name)}[/]",
            subtitle=f"[dim]{request.request_id[:12]}[/]",
            border_style=border,
            expand=False,
            padding=(0, 2),
        )
    )


def _render_question_panel(request: ModelQuestionRequest) -> None:
    """Print a Rich panel for a model question."""
    console.print(
        Panel(
            Text(_sanitize_text(request.question)),
            title=f"[{ACCENT}]{markup_escape(request.source)}[/]",
            title_align="left",
            border_style=ACCENT,
            expand=False,
            padding=(0, 2),
        )
    )


# ---------------------------------------------------------------------------
# Interactive action bar (prompt_toolkit Application)
# ---------------------------------------------------------------------------


async def _run_action_bar(
    actions: list[str],
    labels: list[str],
    *,
    hotkeys: dict[str, str] | None = None,
    default_cancel: str = "deny",
) -> str:
    """Arrow-key selectable action bar. Returns selected action string.

    Parameters
    ----------
    actions:
        Action identifiers (returned on selection).
    labels:
        Display labels for each action.
    hotkeys:
        Optional mapping of single-char keys to action strings.
    default_cancel:
        Action returned on Escape / Ctrl-C.
    """
    if not actions:
        return default_cancel

    selected = [0]
    kb = KeyBindings()

    @kb.add("left")
    def _left(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        selected[0] = max(0, selected[0] - 1)

    @kb.add("right")
    def _right(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        selected[0] = min(len(actions) - 1, selected[0] + 1)

    @kb.add("up")
    def _up(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        selected[0] = max(0, selected[0] - 1)

    @kb.add("down")
    def _down(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        selected[0] = min(len(actions) - 1, selected[0] + 1)

    @kb.add("enter")
    def _accept(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.app.exit(result=actions[selected[0]])

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.app.exit(result=default_cancel)

    # Register hotkeys
    if hotkeys:
        for key, action in hotkeys.items():

            def _make_hotkey_handler(a: str):  # noqa: E301
                def _handler(event: KeyPressEvent) -> None:
                    event.app.exit(result=a)

                return _handler

            kb.add(key)(_make_hotkey_handler(action))

    # Determine layout: horizontal for <=5, vertical for >5
    term_width = shutil.get_terminal_size((80, 24)).columns
    use_vertical = len(actions) > 5 or term_width < 50

    def _get_formatted_text() -> FormattedText:
        parts: list[tuple[str, str]] = []
        if use_vertical:
            for i, label in enumerate(labels):
                style = "class:selected" if i == selected[0] else "class:unselected"
                marker = " \u25b8 " if i == selected[0] else "   "
                parts.append((style, f"{marker}{label}"))
                if i < len(labels) - 1:
                    parts.append(("", "\n"))
        else:
            for i, label in enumerate(labels):
                style = "class:selected" if i == selected[0] else "class:unselected"
                marker = " \u25b8 " if i == selected[0] else "   "
                parts.append((style, f"{marker}{label} "))
        return FormattedText(parts)

    height = len(actions) if use_vertical else 1

    app: Application[str] = Application(
        layout=Layout(
            Window(
                FormattedTextControl(_get_formatted_text),
                height=height,
            )
        ),
        key_bindings=kb,
        style=_WIDGET_STYLE,
        full_screen=False,
    )

    with patch_stdout(raw=True):
        result = await app.run_async()

    return result or default_cancel


async def _run_text_input(placeholder: str = "") -> str:
    """Single-line text input for free-form responses."""
    session: PromptSession[str] = PromptSession()
    try:
        with patch_stdout(raw=True):
            return (await session.prompt_async(f"  \u25b8 ")).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


# ---------------------------------------------------------------------------
# Non-TTY fallback
# ---------------------------------------------------------------------------


def _is_interactive() -> bool:
    """Check if stdin is a TTY (supports interactive widgets)."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


async def _fallback_confirm(prompt_msg: str = "Allow? [y/n/always] ") -> str:
    """Non-TTY fallback using basic prompt."""
    from obscura.cli.prompt import confirm_prompt_async

    return await confirm_prompt_async(prompt_msg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def confirm_tool(request: ToolConfirmRequest) -> WidgetResult:
    """Full TUI tool confirmation widget.

    Shows a Rich panel with tool name and args, then an interactive
    action bar for [Allow] [Deny] [Always Allow].
    """
    if not _is_interactive():
        answer = await _fallback_confirm()
        if answer == "always":
            return WidgetResult(action="always_allow")
        if answer in ("y", "yes"):
            return WidgetResult(action="allow")
        return WidgetResult(action="deny")

    console.print()
    _render_tool_panel(request.tool_name, request.tool_input)

    action = await _run_action_bar(
        actions=["allow", "deny", "always_allow"],
        labels=[f"Allow ({OK_COLOR[0]})", f"Deny ({ERROR_COLOR[0]})", "Always Allow"],
        hotkeys={"y": "allow", "n": "deny", "a": "always_allow"},
        default_cancel="deny",
    )
    return WidgetResult(action=action)


async def confirm_attention(request: AttentionWidgetRequest) -> WidgetResult:
    """Full TUI attention request widget.

    Shows a priority-styled Rich panel with the agent's message,
    then an interactive action bar from the request's actions.
    """
    if not _is_interactive():
        actions = request.actions
        prompt_str = (
            f"  [{'/'.join(actions)}]: "
            if actions and actions != ("ok",)
            else "  [ok]: "
        )
        answer = await _fallback_confirm(prompt_str)
        action = answer if answer in actions else (actions[0] if actions else "ok")
        text = answer if answer not in actions else ""
        return WidgetResult(action=action, text=text)

    console.print()
    _render_attention_panel(request)

    actions_list = list(request.actions)
    labels = [a.replace("_", " ").title() for a in actions_list]

    # Build hotkeys from first char of each action (if unique)
    hotkeys: dict[str, str] = {}
    seen_keys: set[str] = set()
    for action in actions_list:
        key = action[0].lower()
        if key not in seen_keys:
            hotkeys[key] = action
            seen_keys.add(key)

    action = await _run_action_bar(
        actions=actions_list,
        labels=labels,
        hotkeys=hotkeys,
        default_cancel=actions_list[0] if actions_list else "ok",
    )
    return WidgetResult(action=action)


async def ask_model_question(request: ModelQuestionRequest) -> WidgetResult:
    """Widget for when the model asks the user a free-form question.

    Shows a styled Rich panel with the question, then a text input.
    """
    console.print()
    _render_question_panel(request)

    if not _is_interactive():
        answer = await _fallback_confirm("  > ")
        return WidgetResult(action="respond", text=answer)

    text = await _run_text_input()
    return WidgetResult(action="respond", text=text)


# ---------------------------------------------------------------------------
# Auto-detection: numbered-list questions in model text
# ---------------------------------------------------------------------------

import re as _re

# Matches lines like "1. Option A", "2) Option B", "- Option C"
_NUMBERED_ITEM_RE = _re.compile(
    r"^\s*(?:(\d+)[.)]\s+|[-*]\s+)(.+)$",
    _re.MULTILINE,
)

# Question patterns that precede a list of choices
_QUESTION_PATTERNS = [
    _re.compile(r"which\s+(?:one|option|approach)", _re.IGNORECASE),
    _re.compile(r"(?:please\s+)?(?:choose|select|pick)\b", _re.IGNORECASE),
    _re.compile(r"would\s+you\s+(?:like|prefer)\b", _re.IGNORECASE),
    _re.compile(r"do\s+you\s+want\b", _re.IGNORECASE),
    _re.compile(r"here\s+are\s+(?:the\s+)?(?:your\s+)?options\b", _re.IGNORECASE),
    _re.compile(r"what\s+(?:should|would)\b", _re.IGNORECASE),
]


@dataclass(frozen=True)
class DetectedQuestion:
    """A question with selectable choices detected in model text."""

    question: str
    choices: list[str]


def detect_question_choices(text: str) -> DetectedQuestion | None:
    """Detect a numbered-list question pattern in model response text.

    Returns ``None`` if no clear question + choices pattern is found.
    Only triggers on unambiguous patterns to avoid false positives.
    """
    if not text or len(text) < 20:
        return None

    # Find numbered/bulleted items
    items = _NUMBERED_ITEM_RE.findall(text)
    if len(items) < 2 or len(items) > 10:
        return None

    choices = [item[1].strip().rstrip(".") for item in items]

    # Check if there's a question-like sentence near the list
    # Look at the text before the first item
    first_match = _NUMBERED_ITEM_RE.search(text)
    if first_match is None:
        return None

    preamble = text[: first_match.start()].strip()
    if not preamble:
        # Also check text after the list
        last_match_end = 0
        for m in _NUMBERED_ITEM_RE.finditer(text):
            last_match_end = m.end()
        postamble = text[last_match_end:].strip()
        preamble = postamble

    # Must match at least one question pattern
    has_question = any(pat.search(preamble) for pat in _QUESTION_PATTERNS)
    if not has_question:
        # Check if preamble ends with "?"
        has_question = preamble.rstrip().endswith("?")

    if not has_question:
        return None

    # Extract question text (first sentence or line of preamble)
    question_lines = preamble.split("\n")
    question = question_lines[-1].strip() if question_lines else preamble
    if not question:
        question = "Please select an option:"

    return DetectedQuestion(question=question, choices=choices)


async def present_detected_choices(detected: DetectedQuestion) -> str | None:
    """Present auto-detected choices as an interactive widget.

    Returns the selected choice string, or ``None`` if not interactive.
    """
    if not _is_interactive() or not detected.choices:
        return None

    result = await confirm_attention(
        AttentionWidgetRequest(
            request_id="auto_detect",
            agent_name="assistant",
            message=detected.question,
            priority="normal",
            actions=tuple(detected.choices),
        )
    )
    return result.action


# ---------------------------------------------------------------------------
# Permission / Notification widgets
# ---------------------------------------------------------------------------


def _render_permission_panel(request: PermissionWidgetRequest) -> None:
    """Print a Rich panel for a permission request."""
    risk_border = {
        "low": "dim",
        "medium": WARN_COLOR,
        "high": f"bold {WARN_COLOR}",
        "critical": f"bold {ERROR_COLOR}",
    }
    border = risk_border.get(request.risk, WARN_COLOR)

    body_parts: list[str] = []
    body_parts.append(f"[bold]Action:[/] {markup_escape(_sanitize_text(request.action))}")
    if request.reason:
        body_parts.append(f"[bold]Reason:[/] {markup_escape(_sanitize_text(request.reason))}")
    body_parts.append(f"[bold]Risk:[/] {markup_escape(request.risk)}")

    console.print(
        Panel(
            "\n".join(body_parts),
            title=f"[{border}]Permission Request[/]",
            title_align="left",
            border_style=border,
            expand=False,
            padding=(0, 2),
        )
    )


def render_notification_banner(request: NotifyWidgetRequest) -> None:
    """Print a Rich panel for a notification (display-only, no input)."""
    priority_border = {
        "low": "dim",
        "normal": ACCENT,
        "high": f"bold {WARN_COLOR}",
        "critical": f"bold {ERROR_COLOR}",
    }
    border = priority_border.get(request.priority, ACCENT)

    body = markup_escape(_sanitize_text(request.message))

    console.print()
    console.print(
        Panel(
            body,
            title=f"[{border}]{markup_escape(_sanitize_text(request.title))}[/]",
            title_align="left",
            border_style=border,
            expand=False,
            padding=(0, 2),
        )
    )


async def confirm_permission(request: PermissionWidgetRequest) -> WidgetResult:
    """Full TUI permission request widget.

    Shows a risk-styled Rich panel with action/reason, then an interactive
    action bar for [Approve] [Deny].
    """
    if not _is_interactive():
        answer = await _fallback_confirm("Approve? [y/n] ")
        if answer in ("y", "yes"):
            return WidgetResult(action="approve")
        return WidgetResult(action="deny")

    console.print()
    _render_permission_panel(request)

    action = await _run_action_bar(
        actions=["approve", "deny"],
        labels=["Approve", "Deny"],
        hotkeys={"y": "approve", "n": "deny"},
        default_cancel="deny",
    )
    return WidgetResult(action=action)
