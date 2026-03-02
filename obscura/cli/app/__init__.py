"""obscura.cli.app -- CLI application domain models.

Modes, plans, and diff engine used by the CLI REPL.
"""

from obscura.cli.app.diff_engine import DiffEngine, DiffHunk, DiffLine
from obscura.cli.app.diff_engine import FileChange as DiffFileChange
from obscura.cli.app.modes import (
    FileChange,
    ModeManager,
    Plan,
    PlanStep,
    TUIMode,
)

__all__ = [
    "DiffEngine",
    "DiffFileChange",
    "DiffHunk",
    "DiffLine",
    "FileChange",
    "ModeManager",
    "Plan",
    "PlanStep",
    "TUIMode",
]
