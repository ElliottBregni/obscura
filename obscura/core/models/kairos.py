"""Kairos-domain Pydantic models — typed shapes for plan-engine I/O.

The Kairos runtime persists Goals/Plans/Tasks/Checkpoints/Interventions
as frozen dataclasses (see :mod:`obscura.core.kairos.types`). Their
``metadata`` fields remain :class:`collections.abc.Mapping` since they
carry intentional free-form data, but the LLM-driven planning surface
has more structure and benefits from typed validation:

- :class:`PlanResponseTask` — one task entry returned by the planner LLM.
- :class:`PlanResponse` — the full JSON response (``rationale`` + tasks).

Both are :class:`BoundaryModel` subclasses because the planner is a
stochastic LLM whose output may carry forward-compat keys. Validation
errors raise the existing :class:`obscura.core.kairos.errors.PlanningError`
at the call site.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from pydantic import Field

from obscura.core.models._base import BoundaryModel

logger = logging.getLogger(__name__)


class PlanResponseTask(BoundaryModel):
    """A single task as returned by the planner LLM.

    Mirrors the schema documented in
    :mod:`obscura.core.kairos.plan_engine` (the ``_PLANNING_SYSTEM_PROMPT``
    template). All fields are optional except ``title`` so partial
    responses still parse — the planner's invariants live in code that
    consumes the parsed result, not in the model itself.
    """

    title: str
    description: str = ""
    tool_hint: str = ""
    depends_on_indices: tuple[int, ...] = ()


class PlanResponse(BoundaryModel):
    """The full JSON envelope returned by the planner LLM.

    See :func:`PlanEngine._parse_response` — this typed shape replaces the
    previously untyped ``dict[str, Any]`` so the caller can use attribute
    access (``response.tasks``) instead of repeated ``.get`` walks.

    ``extras`` preserves any unrecognized top-level keys for forward-compat
    debugging; callers ignore them in production code.
    """

    rationale: str = ""
    tasks: tuple[PlanResponseTask, ...] = ()
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PlanResponse:
        """Build a typed plan response from a parsed JSON dict.

        Tasks that fail validation are skipped silently — the caller
        decides whether the resulting (potentially short) task list is
        acceptable. Forward-compat keys land in ``extras``.
        """
        rationale = str(raw.get("rationale", "") or "")
        raw_tasks = raw.get("tasks", [])
        if not isinstance(raw_tasks, list):
            raw_tasks = []

        tasks: list[PlanResponseTask] = []
        for entry in cast("list[Any]", raw_tasks):
            if not isinstance(entry, dict):
                continue
            try:
                tasks.append(PlanResponseTask.model_validate(entry))
            except Exception:  # noqa: BLE001 - planner output is untrusted
                logger.debug("suppressed exception in PlanResponse.from_mapping", exc_info=True)
                continue

        known = {"rationale", "tasks"}
        extras = {k: v for k, v in raw.items() if k not in known}
        return cls(rationale=rationale, tasks=tuple(tasks), extras=dict(extras))


__all__ = [
    "PlanResponse",
    "PlanResponseTask",
]
