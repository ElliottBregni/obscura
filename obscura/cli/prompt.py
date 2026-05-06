# pyright: reportPrivateUsage=false
"""obscura.cli.prompt — back-compat shim for the prompt-toolkit widget kit.

The widgets that used to live here are now in ``obscura.cli.promptkit``.
This module remains as a thin re-exporter so existing call sites
(``obscura.cli._repl_loop``, ``obscura.cli._send``, ``obscura.cli.widgets``,
``packages/browser-extension/native-host/obscura_native_host.py``) keep
importing from ``obscura.cli.prompt`` without change.

The native-host bridge monkey-patches
``obscura.cli.prompt.confirm_prompt_async`` at runtime to round-trip
prompts through the browser side panel; that mechanism continues to
work because we expose ``confirm_prompt_async`` here as a regular
module attribute.

New code should prefer importing from ``obscura.cli.promptkit`` directly.
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

# Legacy re-exports that historically lived in this module — the renderer
# owns the canonical implementation; keep them importable from here for
# call sites that haven't migrated.
from obscura.cli.render import _sanitize_text  # pyright: ignore[reportPrivateUsage]
from obscura.cli.ui_primitives import random_thinking_message

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
    "_sanitize_text",
    "animate_spinner",
    "bordered_prompt",
    "confirm_prompt_async",
    "create_prompt_session",
    "expand_preview",
    "expand_thinking",
    "print_separator",
    "print_status_banner",
    "print_turn_separator",
    "random_thinking_message",
]
