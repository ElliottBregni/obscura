"""Central Pydantic model registry for the obscura runtime.

Domain teams populate this package with frozen Pydantic models composed via
the mixins in `_mixins`. This module intentionally re-exports nothing yet —
domain teams will add curated re-exports as their models land. Until then,
import directly from the submodule that owns the model, or from `_base`
for the shared base classes (`ObscuraModel`, `MutableObscuraModel`,
`BoundaryModel`).
"""

from __future__ import annotations

from obscura.core.models._base import (
    BoundaryModel,
    MutableObscuraModel,
    ObscuraModel,
)

__all__ = [
    "BoundaryModel",
    "MutableObscuraModel",
    "ObscuraModel",
]
