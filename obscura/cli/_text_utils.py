"""obscura.cli._text_utils — shared text-sanitisation utilities.

Thin helper module so ANSI-stripping logic lives in one place rather
than being duplicated across :mod:`obscura.cli.render` and
:mod:`obscura.cli.widgets`.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

__all__ = ["sanitize_text"]


def sanitize_text(s: str) -> str:
    """Remove ANSI/escape sequences and control characters from text."""
    if not s:
        return ""
    try:
        # CSI sequences: ESC [ ... final-byte
        cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", s)
        # OSC sequences: ESC ] ... (ST or BEL)
        cleaned = re.sub(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)", "", cleaned)
        # DCS / PM / APC / SOS sequences
        cleaned = re.sub(r"\x1B[PX^_][^\x1B]*(?:\x1B\\|$)", "", cleaned)
        # Lone ESC + one char
        cleaned = re.sub(r"\x1B[@-Z\\-_]", "", cleaned)
        # Bare ESC
        cleaned = re.sub(r"\x1B", "", cleaned)
        # C0 controls (keep TAB \x09, LF \x0A, CR \x0D)
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+", "", cleaned)
    except Exception:
        logger.debug("suppressed exception in sanitize_text", exc_info=True)
        return s


# Back-compat alias — callers that import the private name still work.
_sanitize_text = sanitize_text
