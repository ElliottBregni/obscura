"""Lightweight runtime helpers for CLI components.

This module holds small factory helpers that perform heavier imports and
object construction which should not occur at package import time.
"""

from __future__ import annotations

from typing import Any

_mode_manager_singleton: Any | None = None


def get_mode_manager_instance() -> Any:
    """Create and return a ModeManager instance (singleton).

    The actual ModeManager class lives in obscura.cli.app.modes and is
    imported here lazily to avoid import-time cost in obscura.cli.
    """
    global _mode_manager_singleton
    if _mode_manager_singleton is None:
        from obscura.cli.app.modes import ModeManager, TUIMode

        _mode_manager_singleton = ModeManager(TUIMode.CODE)
    return _mode_manager_singleton
