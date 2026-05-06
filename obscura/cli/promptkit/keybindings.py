"""obscura.cli.promptkit.keybindings — REPL prompt key bindings.

Builds the ``KeyBindings`` table installed on the prompt_toolkit
``PromptSession``:

  * ``Esc + Enter``  — insert a newline (multi-line input).
  * ``Ctrl-P``        — expand the latest assistant preview as full
                        Markdown via ``_expand_preview_action``
                        (key configurable via ``OBSCURA_EXPAND_PREVIEW_KEY``).
  * ``Ctrl-T``        — expand the last reasoning/thinking block via
                        ``_expand_thinking_action``.
  * ``Ctrl-Space``    — push-to-talk voice marker (the REPL intercepts
                        the magic ``__VOICE_RECORD__`` text).

``expand_preview`` and ``expand_thinking`` are public aliases of the
``_*_action`` callables for tests and external callers.

Consumers
---------
* ``obscura.cli.promptkit.session_factory.create_prompt_session``.
* ``obscura.cli.prompt`` (legacy back-compat shim).
"""

from __future__ import annotations

import logging

from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from obscura.cli.render import (
    THINKING_COLOR,
    _active_renderer,  # pyright: ignore[reportPrivateUsage]
    console,
    get_active_text,
)

logger = logging.getLogger(__name__)


def _expand_preview_action() -> None:
    """Print the full accumulated assistant text from the active renderer."""
    try:
        text = get_active_text()
        if not text:
            console.print("[dim]No preview available to expand.[/]")
            return
        console.print()
        console.print(Markdown(text))
        console.print()
    except Exception:
        logger.debug("suppressed exception in _expand_preview_action", exc_info=True)


def _expand_thinking_action() -> None:
    """Print the last thinking block from the active renderer."""
    try:
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
        logger.debug("suppressed exception in _expand_thinking_action", exc_info=True)


def _make_key_bindings(expand_key: str = "c-p") -> KeyBindings:  # pyright: ignore[reportUnusedFunction]
    """Enter submits, Escape+Enter inserts newline for multiline.

    expand_key may be a prompt_toolkit key spec (default Ctrl-P -> 'c-p').
    """
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _insert_newline(event: object) -> None:  # pyright: ignore[reportUnusedFunction]
        assert isinstance(event, KeyPressEvent)
        event.current_buffer.insert_text("\n")

    # Expand preview hotkey
    try:

        @kb.add(expand_key)
        def _expand(event: object) -> None:  # pyright: ignore[reportUnusedFunction]
            _expand_preview_action()
    except Exception:
        # ignore invalid key spec
        logger.debug("suppressed exception in _make_key_bindings", exc_info=True)

    # Expand last thinking block
    @kb.add("c-t")
    def _expand_thinking(event: object) -> None:  # pyright: ignore[reportUnusedFunction]
        _expand_thinking_action()

    # Voice input: Ctrl+Space triggers push-to-talk recording
    @kb.add("c-space")
    def _voice_record(event: object) -> None:  # pyright: ignore[reportUnusedFunction]
        assert isinstance(event, KeyPressEvent)
        buf = event.current_buffer
        # Insert a voice marker that the REPL will intercept.
        buf.text = "__VOICE_RECORD__"
        buf.validate_and_handle()

    return kb


# Public helpers for tests to call expand action
expand_preview = _expand_preview_action
expand_thinking = _expand_thinking_action
