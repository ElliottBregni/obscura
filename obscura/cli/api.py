"""Public, import-safe CLI API for Obscura.

Keep this module minimal and cheap to import. Export a small surface that
other tests and integration points should use instead of importing
obscura.cli.__init__ directly.

Exports:
- REPLContextLite: lightweight typed subset of REPLContext for import-time use
- get_commands_registry(): access the mutable COMMANDS mapping lazily

This module purposely avoids importing heavy runtime subsystems.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Lightweight, import-safe dataclass mirroring a small portion of REPLContext
@dataclass
class REPLContextLite:
    session_id: str | None = None
    backend: str | None = None
    model: str | None = None


# Accessor for the commands registry. Import is deferred to avoid heavy
# dependencies or side-effects at import time.
def get_commands_registry() -> dict[str, Any]:
    """Return the CLI commands registry (COMMANDS).

    This performs a lazy import of obscura.cli.commands and returns the
    mutable registry so callers can inspect or extend it.
    """
    from obscura.cli import commands as _commands

    return getattr(_commands, "COMMANDS")
