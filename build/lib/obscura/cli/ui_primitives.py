"""obscura.cli.ui_primitives — Tiny UI bits shared by render and prompt.

Lives below both ``cli.render`` and ``cli.prompt`` in the layer hierarchy
so either can import from here without forming a peer cycle. Previously
``cli.render._start_thinking`` lazy-imported ``random_thinking_message``
from ``cli.prompt`` to dodge the import-time cycle (``prompt`` already
imports a handful of names from ``render`` at module top).
"""

from __future__ import annotations

import random

_THINKING_MESSAGES = (
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
)


def random_thinking_message() -> str:
    """Return a random thinking status message."""
    return random.choice(_THINKING_MESSAGES)
