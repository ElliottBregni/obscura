"""Central enum registry for the obscura runtime.

Domain teams populate this package with `StrEnum` subclasses grouped by
domain (agent, auth, error, lifecycle, messaging, protocol, storage, tools,
ui). This module intentionally re-exports nothing yet — domain teams will
add curated re-exports as their domains land. Until then, import directly
from the submodule that owns the enum, or from `_base` for shared
infrastructure (`Lifecycle`, `parse_lenient`).
"""

from __future__ import annotations

from obscura.core.enums import _base as _base

__all__: list[str] = []
