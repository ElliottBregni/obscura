"""Centralized runtime configuration for Obscura.

Environment flags documented here and used across the codebase.

Flags:
- OBSCURA_VERBOSE: 'true'|'false' (default: true) — when true, internal verbose output is enabled.
- OBSCURA_OUTPUT_MODE: string (default: 'cli') — controls OutputManager.env.
- OBSCURA_CAPTURE_PRINTS: 'true'|'false' (default: false) — when true, builtins.print is captured into the OutputManager.

Keep this module minimal — other modules should import these constants instead of reading os.environ directly.
"""

from __future__ import annotations

import os
from typing import Any


def _env_flag(name: str, default: bool = False) -> bool:
    """Return True if the env var is set to a truthy string 'true'.

    `default` is the boolean default used when the variable is not present.
    """
    raw = os.environ.get(name, str(default)).strip().lower()
    return raw == "true"


# Public configuration values
# NOTE: defaults chosen to preserve existing behavior where applicable.
VERBOSE: bool = _env_flag("OBSCURA_VERBOSE", True)
OUTPUT_MODE: str = os.environ.get("OBSCURA_OUTPUT_MODE", "cli")
CAPTURE_PRINTS: bool = _env_flag("OBSCURA_CAPTURE_PRINTS", False)


__all__ = ["VERBOSE", "OUTPUT_MODE", "CAPTURE_PRINTS"]
