from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, cast

logger = logging.getLogger(__name__)

_GuardFn = Callable[[Mapping[str, Any]], tuple[bool, str]]


def register_pretool_guard(hooks_manager: object, guard_func: _GuardFn) -> None:
    """Register a PRE_TOOL_USE guard handler.

    The guard_func should accept a single `context` mapping and return
    (allowed: bool, reason: str).

    The hooks_manager is expected to be duck-typed and support one of:
      - bind_handler(name, handler, when='before')
      - register(name); bind(name, handler)
      - events dict mapping name -> list[callables]

    The registered handler will call guard_func and propagate its result.
    """

    def _handler(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        # Expect context to be passed either as first positional arg or as
        # keyword 'context'. Be forgiving.
        context: Any = None
        if args:
            context = args[0]
        context = context or kwargs.get("context") or {}
        try:
            allowed, reason = guard_func(context)
            return allowed, reason
        except Exception:
            logger.exception("pre_tool_use guard failed")
            # Fail safe: veto on unexpected errors
            return False, "vetoed: guard error"

    hm: Any = hooks_manager
    # Try bind_handler API
    if hasattr(hm, "bind_handler"):
        try:
            hm.bind_handler("PRE_TOOL_USE", _handler, when="before")
            return
        except Exception:
            logger.debug(
                "suppressed exception in register_pretool_guard", exc_info=True
            )

    # Try register/bind pair
    if hasattr(hm, "register") and hasattr(hm, "bind"):
        try:
            hm.register("PRE_TOOL_USE")
            hm.bind("PRE_TOOL_USE", _handler)
            return
        except Exception:
            logger.debug(
                "suppressed exception in register_pretool_guard", exc_info=True
            )

    # Attach to events dict
    if hasattr(hm, "events"):
        raw_events: Any = hm.events
        if isinstance(raw_events, dict):
            events = cast(dict[str, list[Any]], raw_events)
            events.setdefault("PRE_TOOL_USE", []).append(_handler)
            return

    raise RuntimeError(
        "Unable to register PRE_TOOL_USE handler on provided hooks_manager"
    )
