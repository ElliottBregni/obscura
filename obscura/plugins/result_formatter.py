"""obscura.plugins.result_formatter — Configurable tool output formatting.

Filters tool results based on the tool's declared output levels and the
agent's requested verbosity.  The broker calls :func:`format_tool_result`
after handler execution and before wrapping the result in a
``ToolResultEnvelope``.

Output levels are declared in ``ToolSpec.output_schema`` using extension keys:

.. code-block:: python

    output_schema = {
        "x-output-levels": {
            "minimal": ["ok"],
            "standard": ["ok", "stdout", "exit_code"],
            "full": ["ok", "stdout", "stderr", "exit_code"],
        },
        "x-default-level": "standard",
    }

When a tool has no ``output_schema`` or the requested level is ``"raw"``,
results pass through unchanged.
"""

from __future__ import annotations

import enum
import json
import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

logger = logging.getLogger(__name__)


class OutputLevel(enum.StrEnum):
    """Named output verbosity levels."""

    MINIMAL = "minimal"
    STANDARD = "standard"
    FULL = "full"
    RAW = "raw"


def format_tool_result(result: Any, spec: ToolSpec, level: str) -> Any:
    """Filter a tool result based on its output schema and the requested level.

    Parameters
    ----------
    result:
        Raw handler return value (str, dict, or other).
    spec:
        The tool's ``ToolSpec`` (must have ``output_schema``).
    level:
        Requested output level (e.g. ``"minimal"``, ``"standard"``).

    Returns
    -------
    Any
        The filtered result, or the original if no filtering applies.
    """
    schema = spec.output_schema
    if not schema or level == OutputLevel.RAW:
        return result

    levels: dict[str, list[str]] = schema.get("x-output-levels", {})
    if not levels:
        return result

    # Resolve the effective level
    if level not in levels:
        default = schema.get("x-default-level", "")
        if default and default in levels:
            level = default
        else:
            return result

    allowed_keys = set(levels[level])

    # Parse string results (system tools return json.dumps strings)
    data = result
    was_string = False
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return result
        was_string = True

    if not isinstance(data, dict):
        return result

    typed_data = cast(dict[str, Any], data)
    filtered: dict[str, Any] = {
        k: v for k, v in typed_data.items() if k in allowed_keys
    }

    # Re-serialize if the input was a string
    if was_string:
        return json.dumps(filtered)
    return filtered
