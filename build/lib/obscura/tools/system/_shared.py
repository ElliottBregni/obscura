"""obscura.tools.system._shared — Decoupling layer for parent↔child references.

Sibling modules under ``obscura.tools.system`` (``_sandbox``, ``_process``)
need to call ``get_system_tool_specs()`` — the aggregator that lives in
``obscura/tools/system/__init__.py``. Importing the parent package from
children created a partial-init cycle: ``__init__`` imports the children,
and the children try to read a name (``get_system_tool_specs``) that
isn't defined until much later in ``__init__``.

Resolution: this module is a leaf (no obscura.tools.system.* deps). It
exposes ``get_system_tool_specs`` and ``set_spec_provider``. The parent
``__init__`` registers its concrete aggregator at the end of its own
load. Children import from here at module top — no cycle, because this
module never reaches into them.

Until the parent registers a provider, ``get_system_tool_specs()``
returns an empty list. That's safe: children only call it inside async
tool handlers, which run after the package is fully loaded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

_SpecProvider = Callable[[], "list[ToolSpec]"]
_provider: _SpecProvider | None = None


def set_spec_provider(provider: _SpecProvider) -> None:
    """Register the aggregator callback. Called once by the parent ``__init__``."""
    global _provider
    _provider = provider


def get_system_tool_specs() -> list[ToolSpec]:
    """Return the system tool specs.

    Returns an empty list if the parent ``__init__`` has not yet
    registered a provider — e.g. while children are being imported.
    """
    if _provider is None:
        return []
    return _provider()
