"""Central Pydantic model registry for the obscura runtime.

Domain teams populate this package with frozen Pydantic models composed via
the mixins in `_mixins`. Boundary teams add I/O-bound models that parse
incoming dicts into typed objects on ingress and round-trip via
``model_dump`` on egress.
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
