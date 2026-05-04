"""obscura.tools.result — boundary `ToolResult` re-export.

The canonical model lives in :mod:`obscura.core.models.tool_result`. This
module re-exports it so existing imports
(``from obscura.tools.result import ToolResult``) keep resolving without
modification.

Legacy chainable-builder usage
(``ToolResult.ok(...).data(...).json()``) is preserved via
:class:`ToolResultBuilder`. New code should construct the boundary
:class:`ToolResult` directly or use the ``success`` / ``failure``
builders.
"""

from __future__ import annotations

from obscura.core.models.tool_result import (
    ToolResult,
    ToolResultBuilder,
)

__all__ = [
    "ToolResult",
    "ToolResultBuilder",
]
