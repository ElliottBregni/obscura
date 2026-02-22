"""Legacy CLI shim.

Older virtualenv scripts may import `sdk.cli:main`. Delegate to the current
`obscura.cli:main` implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from obscura.cli import main
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from obscura.cli import main

__all__ = ["main"]
