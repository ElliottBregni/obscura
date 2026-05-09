# pyright: reportPrivateUsage=false
"""obscura.cli.promptkit.style — prompt_toolkit Style + small helpers.

Owns the shared ``PROMPT_STYLE`` (merging the keyword gradient classes
from ``highlighter.py``) plus the prompt-message factory and the
inter-turn separator helpers.

Consumers
---------
* ``obscura.cli.promptkit.session_factory.create_prompt_session`` —
  passes ``PROMPT_STYLE`` to ``PromptSession``.
* ``obscura.cli.prompt`` (legacy back-compat shim).
* ``obscura.cli.tui`` — full-screen Textual TUI under construction.
"""

from __future__ import annotations

import sys

from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style

from obscura.cli.promptkit.highlighter import _keyword_gradient_styles
from obscura.cli.renderer.modern.theme import (
    BLUE as _C_BLUE,
    LAVENDER as _C_LAVENDER,
    OVERLAY0 as _C_OVERLAY0,
    SUBTEXT0 as _C_SUBTEXT0,
    SURFACE1 as _C_SURFACE1,
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

_RULE_CHAR = "\u2500"


def print_separator() -> None:
    """Print a subtle separator between turns."""
    sys.stdout.write("\n")
    sys.stdout.flush()


def print_turn_separator() -> None:
    """Print a thin visual break between turns — just breathing room."""
    sys.stdout.write("\n")
    sys.stdout.flush()
