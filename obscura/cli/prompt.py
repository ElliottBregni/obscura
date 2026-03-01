"""obscura.cli.prompt — prompt_toolkit-based input for the REPL.

Provides a modern, responsive input experience with auto-suggestions,
slash-command completion, multiline support, bordered input, and
concurrent input during streaming.
"""

from __future__ import annotations

import shutil
import os
from typing import override

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from obscura.core.paths import resolve_obscura_home


# ---------------------------------------------------------------------------
# Slash-command completer
# ---------------------------------------------------------------------------


class SlashCommandCompleter(Completer):
    """Tab-complete /commands and their subcommands from COMPLETIONS dict."""

    def __init__(self, completions: dict[str, list[str]]) -> None:
        self._completions = completions

    @override
    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> list[Completion]:
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return []

        parts = text.split()

        # Completing the command name: "/he" -> "/help"
        if len(parts) <= 1:
            prefix = text[1:]  # strip leading "/"
            for cmd in sorted(self._completions):
                if cmd.startswith(prefix):
                    yield Completion(
                        "/" + cmd,
                        start_position=-len(text),
                        display="/" + cmd,
                    )
            return

        # Completing subcommands: "/backend co" -> "copilot"
        cmd = parts[0].lstrip("/")
        subs = self._completions.get(cmd, [])
        if not subs:
            return

        partial = parts[1] if len(parts) > 1 else ""
        for sub in sorted(subs):
            if sub.startswith(partial):
                yield Completion(sub, start_position=-len(partial))
        return


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "#6c71c4 bold",
        "continuation": "#586e75",
        "bottom-toolbar": "bg:#1a1a2e #586e75",
    }
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
        from obscura.cli.render import get_active_text, console
        from rich.markdown import Markdown

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

    return kb


# Public helper for tests to call expand action
expand_preview = _expand_preview_action


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


def create_prompt_session(
    completions: dict[str, list[str]],
    toolbar_text: str = "",
) -> PromptSession[str]:
    """Create a configured PromptSession for the Obscura REPL."""
    # Ensure the Obscura home directory exists so FileHistory can write.
    home = resolve_obscura_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    history_path = home / "cli_history_v2"

    bottom_toolbar: HTML | None = None
    if toolbar_text:
        bottom_toolbar = HTML(f"  {toolbar_text}")

    session: PromptSession[str] = PromptSession(
        message=_make_prompt_message(),
        style=PROMPT_STYLE,
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=SlashCommandCompleter(completions),
        complete_while_typing=False,
        key_bindings=_make_key_bindings(os.environ.get("OBSCURA_EXPAND_PREVIEW_KEY", "c-p")),
        enable_history_search=True,
        mouse_support=False,
        prompt_continuation="  \u00b7 ",
        bottom_toolbar=bottom_toolbar,
    )
    return session


# ---------------------------------------------------------------------------
# Bordered prompt (separator + prompt + separator)
# ---------------------------------------------------------------------------


async def bordered_prompt(session: PromptSession[str]) -> str:
    """Show separator, prompt for input, return stripped input with a small buffer after.

    Use a single separator to mark the input area and a short blank line after the prompt to
    avoid very aggressive horizontal rules that break flow.
    """
    print_separator()
    with patch_stdout(raw=True):
        result = await session.prompt_async()
    # add a small blank line after input instead of a full separator to reduce visual noise
    print()
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
