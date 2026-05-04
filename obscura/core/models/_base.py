"""Base Pydantic models for in-memory and boundary types.

Three flavors:

- `ObscuraModel` — frozen, strict, `extra="forbid"`. The default for every
  internal value object. Mutation is rejected at construction; unknown
  fields are a hard error so we catch wire-format drift at the seam.
- `MutableObscuraModel` — same configuration but `frozen=False`. Reserved
  for long-lived records that genuinely mutate in place (Task, Goal,
  Approval, ...) where copy-on-write would force every consumer to rebind.
- `BoundaryModel` — used at I/O seams (JSON-RPC frames, plugin manifests,
  persisted JSON snapshots). Tolerates `extra="ignore"` so forward-compat
  payloads parse cleanly, and accepts both field names and aliases.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ObscuraModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class MutableObscuraModel(BaseModel):
    model_config = ConfigDict(
        frozen=False,
        extra="forbid",
        strict=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class BoundaryModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=False,
        populate_by_name=True,
    )
