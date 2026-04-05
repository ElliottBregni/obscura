from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


def register_pretool_guard(hooks_manager: object, guard_func: Callable) -> None:
    """Register a PRE_TOOL_USE guard handler.

    The guard_func should accept a single `context` mapping and return
    (allowed: bool, reason: str).

    The hooks_manager is expected to be duck-typed and support one of:
      - bind_handler(name, handler, when='before')
      - register(name); bind(name, handler)
      - events dict mapping name -> list[callables]

    The registered handler will call guard_func and propagate its result.
    """

    def _handler(*args, **kwargs):
        # Expect context to be passed either as first positional arg or as
        # keyword 'context'. Be forgiving.
        context = None
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

    # Try bind_handler API
    if hasattr(hooks_manager, "bind_handler"):
        try:
            hooks_manager.bind_handler("PRE_TOOL_USE", _handler, when="before")
            return
        except Exception:
            pass

    # Try register/bind pair
    if hasattr(hooks_manager, "register") and hasattr(hooks_manager, "bind"):
        try:
            hooks_manager.register("PRE_TOOL_USE")
            hooks_manager.bind("PRE_TOOL_USE", _handler)
            return
        except Exception:
            pass

    # Attach to events dict
    if hasattr(hooks_manager, "events") and isinstance(hooks_manager.events, dict):
        hooks_manager.events.setdefault("PRE_TOOL_USE", []).append(_handler)
        return

    raise RuntimeError("Unable to register PRE_TOOL_USE handler on provided hooks_manager")
