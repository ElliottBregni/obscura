# pyright: reportPrivateUsage=false
"""obscura.cli.promptkit — shared prompt_toolkit widget kit.

This package holds the prompt_toolkit-based widget building blocks that
power both:

  * the legacy bordered REPL (``obscura.cli.prompt`` is a back-compat
    shim re-exporting from this package), and
  * the new full-screen Textual TUI under construction in
    ``obscura.cli.tui``.

Splitting the widgets out of ``prompt.py`` lets both consumers share one
implementation without code duplication.  No behaviour change vs. the
pre-split ``prompt.py``.

Public API exported here mirrors what ``obscura.cli.prompt`` historically
exposed; consumers should prefer importing from this package directly,
but the legacy ``obscura.cli.prompt`` module continues to work.
"""

from __future__ import annotations

from obscura.cli.promptkit.completer import SlashCommandCompleter
from obscura.cli.promptkit.highlighter import (
    KeywordHighlighter,
    _GRADIENT,
    _HIGHLIGHT_KEYWORDS,
    _keyword_gradient_styles,
)
from obscura.cli.promptkit.keybindings import (
    _expand_preview_action,
    _expand_thinking_action,
    _make_key_bindings,
    expand_preview,
    expand_thinking,
)
from obscura.cli.promptkit.session_factory import (
    PromptHUDState,
    PromptLayoutConfig,
    _build_prompt_message_html,
    _render_menu_line,
    _render_model_status_line,
    bordered_prompt,
    confirm_prompt_async,
    create_prompt_session,
)
from obscura.cli.promptkit.status import (
    PromptStatus,
    RunningAgentInfo,
    StreamingStatus,
    _build_toolbar_html,
    _get_git_branch,
    animate_spinner,
    print_status_banner,
)
from obscura.cli.promptkit.style import (
    PROMPT_STYLE,
    _RULE_CHAR,
    _make_prompt_message,
    print_separator,
    print_turn_separator,
)

__all__ = [
    "PROMPT_STYLE",
    "KeywordHighlighter",
    "PromptHUDState",
    "PromptLayoutConfig",
    "PromptStatus",
    "RunningAgentInfo",
    "SlashCommandCompleter",
    "StreamingStatus",
    "_GRADIENT",
    "_HIGHLIGHT_KEYWORDS",
    "_RULE_CHAR",
    "_build_prompt_message_html",
    "_build_toolbar_html",
    "_expand_preview_action",
    "_expand_thinking_action",
    "_get_git_branch",
    "_keyword_gradient_styles",
    "_make_key_bindings",
    "_make_prompt_message",
    "_render_menu_line",
    "_render_model_status_line",
    "animate_spinner",
    "bordered_prompt",
    "confirm_prompt_async",
    "create_prompt_session",
    "expand_preview",
    "expand_thinking",
    "print_separator",
    "print_status_banner",
    "print_turn_separator",
]
