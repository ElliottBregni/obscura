"""Compatibility shims for Codex SDK / CLI version skew."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# The pinned ``codex_app_server`` SDK declares some response fields as
# required that newer ``codex`` CLI binaries no longer emit (and vice
# versa). We don't read these fields ourselves, so relaxing them avoids
# startup/runtime failures across CLI and SDK releases.
_OPTIONAL_RELAXATIONS: dict[str, tuple[str, ...]] = {
    "ThreadStartResponse": ("approvals_reviewer",),
    "ThreadResumeResponse": ("approvals_reviewer",),
    "Thread": ("ephemeral", "status"),
}

# Models that embed ``Thread`` (or another relaxed model) as a nested field.
# Pydantic v2 caches inner validators, so parent models need a rebuild too.
_RELAX_PARENT_REBUILDS: tuple[str, ...] = (
    "ThreadStartResponse",
    "ThreadResumeResponse",
    "ThreadForkResponse",
    "ThreadReadResponse",
    "ThreadUnarchiveResponse",
)


def relax_strict_response_models(mod: Any) -> None:
    """Make selected SDK response fields optional to tolerate version skew."""
    candidates: list[Any] = [mod]
    generated = getattr(mod, "generated", None)
    if generated is not None:
        candidates.append(generated)
        v2 = getattr(generated, "v2_all", None)
        if v2 is not None:
            candidates.append(v2)

    def _resolve(model_name: str) -> Any:
        return next(
            (
                getattr(c, model_name)
                for c in candidates
                if hasattr(c, model_name)
                and hasattr(getattr(c, model_name), "model_fields")
            ),
            None,
        )

    any_changed = False
    for model_name, field_names in _OPTIONAL_RELAXATIONS.items():
        cls = _resolve(model_name)
        if cls is None:
            continue
        changed = False
        for fname in field_names:
            field = cls.model_fields.get(fname)
            if field is None or not field.is_required():
                continue
            field.default = None
            changed = True
        if changed:
            any_changed = True
            try:
                cls.model_rebuild(force=True)
            except Exception:
                logger.debug(
                    "suppressed exception in relax_strict_response_models",
                    exc_info=True,
                )

    if any_changed:
        for parent_name in _RELAX_PARENT_REBUILDS:
            parent = _resolve(parent_name)
            if parent is None:
                continue
            try:
                parent.model_rebuild(force=True)
            except Exception:
                logger.debug(
                    "suppressed exception in relax_strict_response_models",
                    exc_info=True,
                )
