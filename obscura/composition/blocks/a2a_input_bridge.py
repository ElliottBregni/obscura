"""obscura.composition.blocks.a2a_input_bridge — A2A INPUT_REQUIRED bridge.

A2A tasks expose ``INPUT_REQUIRED`` state when an agent needs user input.
Today, only the tool-call confirmation gate (``on_confirm``) drives that
state machine. Tools that read ``current_tool_context().ask_user_callback``
(e.g. the system ``ask_user`` tool, ``enter_plan_mode``,
``exit_plan_mode``) silently no-op when invoked from A2A because the
context callbacks aren't set.

This block fixes that. It wires three host_callbacks into the agent's
``ToolContext`` so they all drive the same A2A INPUT_REQUIRED machine:

- ``ask_user_callback``       — free-text answer captured verbatim
- ``permission_mode_callback`` — sync best-effort (no parking; logs)
- ``plan_approval_callback``  — y/n parking via ``_make_plan_approval``

The actual parking + resume logic lives on ``A2AService`` (see
``_park_for_input`` and ``_resume_task``); this block is just the wiring.

Reads:
    session.surface (a2a only)
    Caller-supplied A2AService factory functions (passed as kwargs by
    the A2A service when it builds the session — see service.py
    ``_execute_agent``).

Writes:
    session.host_callbacks['ask_user_callback']
    session.host_callbacks['permission_mode_callback']
    session.host_callbacks['plan_approval_callback']

Resources: none (the parking machinery is owned by A2AService).

Opt-out:
    session.surface != "a2a" → return immediately
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession

logger = logging.getLogger(__name__)


async def install_a2a_input_bridge(
    session: AgentSession,
    *,
    ask_user: Callable[..., Awaitable[str]] | None = None,
    plan_approval: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Wire A2A INPUT_REQUIRED-backed callbacks into the session.

    The caller (A2AService) supplies ``ask_user`` and ``plan_approval``
    factories already bound to the task id — the block just stuffs
    them into ``session.host_callbacks`` so the agent loop's
    ``ToolContext`` builder picks them up.

    See module docstring for full contract.
    """
    if session.surface != "a2a":
        return

    if ask_user is not None:
        session.host_callbacks["ask_user_callback"] = ask_user

    if plan_approval is not None:
        session.host_callbacks["plan_approval_callback"] = plan_approval

    # permission_mode_callback isn't bridged today — A2A tasks don't have
    # a long-running plan-mode state machine the way REPL does. The
    # agent's permission_modes engine still runs locally; the callback
    # just logs the requested mode for debuggability.
    def _permission_mode(mode: str) -> None:
        logger.debug(
            "a2a_input_bridge: permission_mode requested=%s task=%s",
            mode,
            session.session_id[:12],
        )

    session.host_callbacks["permission_mode_callback"] = _permission_mode

    logger.info(
        "a2a_input_bridge: wired ask_user=%s plan_approval=%s",
        bool(ask_user),
        bool(plan_approval),
    )


# Re-export for blocks/__init__.py — the conventional `(session, config)`
# signature isn't appropriate here because A2AService passes the factory
# callbacks directly. See composition/a2a.py for invocation.
__all__ = ["install_a2a_input_bridge"]


# Suppress unused-import warning when the file is imported under
# TYPE_CHECKING-only mode without the AgentSession reference resolving
_ = (Any,)  # keep `Any` import touched for runtime forward-compat
