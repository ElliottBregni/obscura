"""obscura.cli.renderer.modern.alt_screen — Alternate screen buffer manager.

Provides enter/exit for the terminal's alternate screen buffer.
When active, the renderer owns the entire viewport and can use
cursor-addressed rendering.  When inactive (default), rendering
is append-only for ``patch_stdout`` compatibility.
"""

from __future__ import annotations

import os
import sys
from typing import IO, Self


class AltScreenManager:
    """Toggle alternate screen buffer for full-screen mode.

    Controlled via ``OBSCURA_FULLSCREEN=true`` env var or ``/fullscreen``
    command at runtime.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def enter(self) -> None:
        """Switch to alternate screen buffer."""
        if self._active:
            return
        self._stream.write("\033[?1049h")  # smcup — enter alt screen
        self._stream.write("\033[2J")  # clear screen
        self._stream.write("\033[H")  # cursor to top-left
        self._stream.flush()
        self._active = True

    def exit(self) -> None:
        """Return to main screen buffer."""
        if not self._active:
            return
        self._stream.write("\033[?1049l")  # rmcup — exit alt screen
        self._stream.flush()
        self._active = False

    def __enter__(self) -> Self:
        self.enter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.exit()

    @staticmethod
    def should_start_fullscreen() -> bool:
        """Check if fullscreen mode should be enabled on startup."""
        return os.environ.get("OBSCURA_FULLSCREEN", "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
