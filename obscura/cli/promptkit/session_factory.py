# pyright: reportPrivateUsage=false
"""obscura.cli.promptkit.session_factory — PromptSession assembly + prompts.

Wires the highlighter, completer, key bindings, style, and status
widgets from the rest of the package into:

  * ``create_prompt_session`` — returns a fully configured
    ``prompt_toolkit.PromptSession`` for the legacy bordered REPL and
    the new TUI's text-input widget.
  * ``bordered_prompt`` — async one-shot wrapper that calls
    ``session.prompt_async()`` under ``patch_stdout`` and sanitises
    the result.
  * ``confirm_prompt_async`` — minimal async confirm prompt
    (used by tool-confirmation dialogs and monkey-patched by the
    browser-extension native host to round-trip through the side panel).

Also owns the small HUD-layout dataclasses + helpers used by callers
that build a static menu/status line (``PromptLayoutConfig``,
``PromptHUDState``, ``_render_model_status_line``, ``_render_menu_line``,
``_build_prompt_message_html``).

Consumers
---------
* ``obscura.cli._repl_loop`` (constructs the session per REPL turn).
* ``obscura.cli.widgets`` (uses ``confirm_prompt_async`` for tool prompts).
* ``packages/browser-extension/native-host/obscura_native_host.py``
  (monkey-patches ``confirm_prompt_async`` on the legacy
  ``obscura.cli.prompt`` shim).
* ``obscura.cli.prompt`` (legacy back-compat shim).
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from obscura.cli.promptkit.completer import SlashCommandCompleter
from obscura.cli.promptkit.highlighter import KeywordHighlighter
from obscura.cli.promptkit.keybindings import _make_key_bindings
from obscura.cli.promptkit.status import (
    PromptStatus,
    StreamingStatus,
    _build_toolbar_html,
)
from obscura.cli.promptkit.style import PROMPT_STYLE
from obscura.cli.render import (
    _sanitize_text,  # pyright: ignore[reportPrivateUsage]
    get_model_space_delta,
)
from obscura.core.paths import resolve_obscura_home

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HUD layout types
# ---------------------------------------------------------------------------


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


def _build_prompt_message_html(  # pyright: ignore[reportUnusedFunction]
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


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


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

        # Idle: clean prompt - no borders, no decoration
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
                    model_text = get_model_space_delta()
                except Exception:
                    logger.debug(
                        "suppressed exception in create_prompt_session", exc_info=True
                    )
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
            logger.debug("suppressed exception in create_prompt_session", exc_info=True)
            _static_hud_html = None

    def _toolbar() -> HTML:
        if _static_hud_html is not None:
            return HTML(_static_hud_html)
        if _prompt_status is not None or (
            _status is not None and _status.active
        ):
            return HTML(_build_toolbar_html(_prompt_status, _status))
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
        prompt_continuation="  · ",
        bottom_toolbar=_toolbar,
    )
    return session


# ---------------------------------------------------------------------------
# Bordered prompt + confirm prompt
# ---------------------------------------------------------------------------


async def bordered_prompt(
    session: PromptSession[str],
    status: PromptStatus | None = None,  # kept for call-site compat
) -> str:
    """Prompt for user input.

    When Textual TUI is active, awaits the Textual Input widget queue.
    Otherwise uses prompt_toolkit with the \u276f prompt.
    """
    with patch_stdout(raw=True):
        result = await session.prompt_async()
    return _sanitize_text(result).strip()


async def confirm_prompt_async(message: str = "Allow? [y/n/always] ") -> str:
    """Async one-shot prompt for tool confirmation."""
    session: PromptSession[str] = PromptSession()
    try:
        # Wrap with patch_stdout to avoid interleaved prints when other tasks log.
        with patch_stdout(raw=True):
            return (await session.prompt_async(message)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        logger.debug("suppressed exception in confirm_prompt_async", exc_info=True)
        return "n"
