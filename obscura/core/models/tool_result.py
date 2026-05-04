"""Boundary model for tool execution results.

Replaces the chainable :class:`obscura.tools.result.ToolResult` builder with
a Pydantic boundary model that's serialised on egress via
:meth:`pydantic.BaseModel.model_dump`. The legacy module re-exports this
class so call sites keep working.

Builder back-compat: :meth:`ToolResult.success` and :meth:`ToolResult.failure`
return immutable copies (boundary models are frozen) populated with the
caller's fields. The full set of legacy chainable methods (``set``, ``data``,
``context``, ``truncated``, ``timed``, ``json``) is preserved as
:class:`ToolResultBuilder` for callers that genuinely need fluent
construction; new code should call ``ToolResult(...)`` directly or use the
``success`` / ``failure`` constructors.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Self

from pydantic import Field

from obscura.core.models._base import BoundaryModel


class ToolResult(BoundaryModel):
    """Structured tool output.

    ``data`` is intentionally ``Any`` because tool results are heterogeneous
    JSON: a file-read tool returns text, a JSON-query tool returns parsed
    objects, an exec tool returns structured stdout/stderr/exit_code blocks.
    The boundary here is precisely the seam where typed fields end and
    plugin payloads begin — see ``ToolResultBuilder`` for the legacy chainable
    construction path.
    """

    ok: bool
    error: str | None = None
    data: Any | None = None  # noqa: ANN401  # wire format: tool-specific payload
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    extra: Mapping[str, Any] = Field(default_factory=dict)  # noqa: ANN401  # wire format: plugin payload overflow

    @classmethod
    def success(cls, data: Any | None = None, **fields: Any) -> Self:  # noqa: ANN401  # wire format: tool-specific payload
        """Build a successful result. Unknown kwargs land on ``extra``."""
        known: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        valid = set(cls.model_fields.keys())
        for key, value in fields.items():
            if key in valid:
                known[key] = value
            else:
                extra[key] = value
        return cls(ok=True, data=data, extra=extra, **known)

    @classmethod
    def failure(cls, error: str, **fields: Any) -> Self:  # noqa: ANN401  # wire format: legacy kwargs surface
        """Build an error result. Unknown kwargs land on ``extra``."""
        known: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        valid = set(cls.model_fields.keys())
        for key, value in fields.items():
            if key in valid:
                known[key] = value
            else:
                extra[key] = value
        return cls(ok=False, error=error, extra=extra, **known)

    def to_payload(self) -> dict[str, Any]:
        """Flatten ``extra`` into the top-level dict for wire egress.

        ``extra`` exists so the model stays strictly typed for the canonical
        keys while preserving the legacy "anything goes" payload shape that
        plugin handlers depend on. The wire format is the union of both —
        ``extra`` keys never collide with canonical keys because
        :meth:`success` / :meth:`failure` route them apart.
        """
        payload = self.model_dump(exclude_none=True, exclude={"extra"})
        payload.update(dict(self.extra))
        return payload


class ToolResultBuilder:
    """Chainable builder kept for legacy ``ToolResult.ok(...).data(...).json()`` callers.

    New code should construct :class:`ToolResult` directly. This builder
    exists only so the previous public surface keeps compiling while
    consumers migrate.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload: dict[str, Any] = payload or {}

    @classmethod
    def ok(cls, **fields: Any) -> ToolResultBuilder:  # noqa: ANN401  # wire format: legacy kwargs surface
        return cls({"ok": True, **fields})

    @classmethod
    def fail(cls, error: str, **fields: Any) -> ToolResultBuilder:  # noqa: ANN401  # wire format: legacy kwargs surface
        return cls({"ok": False, "error": error, **fields})

    def set(self, **fields: Any) -> ToolResultBuilder:  # noqa: ANN401  # wire format: legacy kwargs surface
        self._payload.update(fields)
        return self

    def context(self, **fields: Any) -> ToolResultBuilder:  # noqa: ANN401  # wire format: legacy kwargs surface
        self._payload.update(fields)
        return self

    def data(self, **fields: Any) -> ToolResultBuilder:  # noqa: ANN401  # wire format: legacy kwargs surface
        self._payload.update(fields)
        return self

    def truncated(
        self,
        is_truncated: bool = True,
        *,
        full_count: int | None = None,
        full_size: int | None = None,
        limit: int | None = None,
    ) -> ToolResultBuilder:
        self._payload["truncated"] = is_truncated
        if full_count is not None:
            self._payload["full_count"] = full_count
        if full_size is not None:
            self._payload["full_size"] = full_size
        if limit is not None:
            self._payload["limit"] = limit
        return self

    def timed(self, start: float) -> ToolResultBuilder:
        self._payload["duration_seconds"] = round(time.monotonic() - start, 2)
        return self

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)

    def to_model(self) -> ToolResult:
        """Coerce the builder's payload into a :class:`ToolResult` boundary model."""
        payload = dict(self._payload)
        ok = bool(payload.pop("ok", False))
        if ok:
            return ToolResult.success(**payload)
        error = str(payload.pop("error", "unknown_error"))
        return ToolResult.failure(error, **payload)

    def json(self) -> str:
        return json.dumps(self._payload)


__all__ = [
    "ToolResult",
    "ToolResultBuilder",
]
