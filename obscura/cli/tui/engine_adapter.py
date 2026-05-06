"""obscura.cli.tui.engine_adapter — TUI-shaped engine bootstrap.

The TUI does NOT replace the engine. It shares the same composition
pipeline as the legacy REPL — :func:`obscura.composition.repl.build_repl_session`
— and only differs in the **host callbacks** it injects: ask-user,
plan-approval, and user-interact go through TUI overlay floats instead
of the inline Rich panels used by the Click REPL.

This module owns:
* :class:`TUIEngineConfig` — Pydantic-typed startup options.
* :class:`TUIEngineHandle` — frozen-by-convention handle returned to the
  app once the session is built.
* :func:`bootstrap_tui_session` — builds the session with TUI callbacks.

No lazy imports. Every dependency is at module top.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from obscura.auth.cli_user import current_cli_user
from obscura.auth.models import AuthenticatedUser
from obscura.composition.repl import build_repl_session
from obscura.composition.session import AgentSession, SessionConfig, SessionExtras
from obscura.core.types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "TUIAskUserCallback",
    "TUIEngineConfig",
    "TUIEngineHandle",
    "TUIPermissionModeCallback",
    "TUIPlanApprovalCallback",
    "TUIUserInteractCallback",
    "bootstrap_tui_session",
]


# ---------------------------------------------------------------------------
# Callback signatures — TUI overlays implement these
# ---------------------------------------------------------------------------

# All callbacks are async; the TUI overlay yields control to the
# Application's event loop while waiting for user interaction.

TUIAskUserCallback = Callable[[str], Awaitable[str]]
"""Show a one-shot text-input float; return the user's typed answer."""

TUIPlanApprovalCallback = Callable[[str], Awaitable[bool]]
"""Show a sticky plan-approval banner; return True iff the user accepts."""

TUIUserInteractCallback = Callable[[str, list[str]], Awaitable[str]]
"""Show a multiple-choice float (message + action labels); return the
chosen action label, or "" if cancelled."""

TUIPermissionModeCallback = Callable[[str], Awaitable[None]]
"""Notify the engine that the user toggled permission mode in the TUI."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TUIEngineConfig(BaseModel):
    """Pydantic-typed TUI startup configuration.

    Mirrors the Click options on ``obscura tui`` exactly. Anything that
    eventually flows into :class:`SessionConfig` lives here as a typed
    field — no untyped ``**kwargs`` survival of the entry point.
    """

    model_config = ConfigDict(frozen=True)

    backend: str = "copilot"
    model: str | None = None
    system: str = ""
    session_id: str | None = None
    max_turns: int = 10
    tools_enabled: bool = True
    confirm_enabled: bool = False
    no_default_prompt: bool = False
    supervise: bool = True
    workspace: str | None = None

    # TUI-only knobs.
    full_screen: bool = True
    """When False, the runtime falls back to the legacy bordered REPL —
    useful for dumb terminals and CI smoke tests."""

    show_thinking: bool = True
    """Whether THINKING_DELTA events are rendered inline. Off → reasoning
    is hidden but still captured for ``Ctrl-T`` expansion."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------


class TUIEngineHandle(BaseModel):
    """Built session + bound callbacks. Owned by :class:`ObscuraTUIApp`.

    ``arbitrary_types_allowed`` is needed because :class:`AgentSession`
    is a stdlib dataclass (not Pydantic) and the callback fields are
    plain callables.
    """

    model_config = ConfigDict(frozen=False, arbitrary_types_allowed=True)

    config: TUIEngineConfig
    session: SkipValidation[AgentSession]
    user: SkipValidation[AuthenticatedUser]

    # Callback slots — set by the runtime once overlays exist, read by
    # composition blocks via the host_callbacks dict on the session.
    ask_user_cb: TUIAskUserCallback | None = None
    plan_approval_cb: TUIPlanApprovalCallback | None = None
    user_interact_cb: TUIUserInteractCallback | None = None
    permission_mode_cb: TUIPermissionModeCallback | None = None

    # Live mutables threaded through to the engine.
    cancel_event: SkipValidation[asyncio.Event] = Field(default_factory=asyncio.Event)

    @property
    def session_id(self) -> str:
        return self.session.session_id

    def submit(self, prompt: str) -> Any:
        """Return ``AsyncIterator[AgentEvent]`` for the prompt.

        The runtime iterates this and feeds events to the renderer.
        Cancellation is via ``self.cancel_event.set()``.
        """
        return self.session.stream_loop(prompt)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _new_session_id(existing: str | None) -> str:
    return existing or uuid.uuid4().hex


def _build_host_callbacks(
    *,
    ask_user_cb: TUIAskUserCallback | None,
    plan_approval_cb: TUIPlanApprovalCallback | None,
    user_interact_cb: TUIUserInteractCallback | None,
    permission_mode_cb: TUIPermissionModeCallback | None,
) -> dict[str, Any]:
    """Assemble the ``host_callbacks`` dict consumed by AgentLoopV2.

    Keys match what ``ToolContext`` reads — see
    ``obscura/core/tool_context.py``. Unset slots are simply omitted so
    composition blocks can still bind their own defaults.
    """
    out: dict[str, Any] = {}
    if ask_user_cb is not None:
        out["ask_user_callback"] = ask_user_cb
    if plan_approval_cb is not None:
        out["plan_approval_callback"] = plan_approval_cb
    if user_interact_cb is not None:
        out["user_interact_callback"] = user_interact_cb
    if permission_mode_cb is not None:
        out["permission_mode_callback"] = permission_mode_cb
    return out


def _to_session_config(
    cfg: TUIEngineConfig,
    *,
    mcp_servers: list[dict[str, Any]],
) -> SessionConfig:
    """Project :class:`TUIEngineConfig` onto :class:`SessionConfig`."""
    extras: SessionExtras = {
        "supervise": cfg.supervise,
        "no_default_prompt": cfg.no_default_prompt,
    }
    return SessionConfig(
        backend=cfg.backend,
        model=cfg.model,
        system_prompt=cfg.system,
        tools_enabled=cfg.tools_enabled,
        confirm_enabled=cfg.confirm_enabled,
        max_turns=cfg.max_turns,
        mcp_servers=mcp_servers,
        extras=extras,
    )


async def bootstrap_tui_session(
    cfg: TUIEngineConfig,
    *,
    mcp_servers: list[dict[str, Any]] | None = None,
    ask_user_cb: TUIAskUserCallback | None = None,
    plan_approval_cb: TUIPlanApprovalCallback | None = None,
    user_interact_cb: TUIUserInteractCallback | None = None,
    permission_mode_cb: TUIPermissionModeCallback | None = None,
) -> TUIEngineHandle:
    """Build a TUI-shaped session via the existing composition pipeline.

    The TUI app calls this once at startup, holds the returned handle
    for the lifetime of the session, and calls ``handle.session.aclose()``
    on exit (via the AgentSession async context manager).

    The four optional callbacks are the **only** TUI-specific divergence
    from the legacy REPL bootstrap. They are passed through as
    ``host_callbacks`` so any tool that reads
    ``current_tool_context().ask_user_callback`` etc. routes through
    the modal float instead of the Rich panel.
    """
    sid = _new_session_id(cfg.session_id)
    user = current_cli_user()
    host_callbacks = _build_host_callbacks(
        ask_user_cb=ask_user_cb,
        plan_approval_cb=plan_approval_cb,
        user_interact_cb=user_interact_cb,
        permission_mode_cb=permission_mode_cb,
    )
    session_config = _to_session_config(cfg, mcp_servers=list(mcp_servers or []))

    logger.info(
        "tui: bootstrapping session sid=%s backend=%s model=%s",
        sid,
        cfg.backend,
        cfg.model or "(default)",
    )

    session = await build_repl_session(
        session_config,
        user=user,
        host_callbacks=host_callbacks,
        session_id=sid,
    )

    return TUIEngineHandle(
        config=cfg,
        session=session,
        user=user,
        ask_user_cb=ask_user_cb,
        plan_approval_cb=plan_approval_cb,
        user_interact_cb=user_interact_cb,
        permission_mode_cb=permission_mode_cb,
    )


async def stream_one_turn(
    handle: TUIEngineHandle,
    prompt: str,
    *,
    on_event: Callable[[AgentEvent], Awaitable[None]],
) -> None:
    """Drive one user turn end-to-end, awaiting ``on_event`` for each event.

    ``on_event`` is the renderer entry point (``await renderer.handle(ev)``).
    Returns when the agent emits ``AGENT_DONE`` or the cancel event fires.
    """
    async for event in handle.submit(prompt):
        if handle.cancel_event.is_set():
            logger.info("tui: cancel_event set, breaking turn early")
            handle.cancel_event.clear()
            return
        await on_event(event)
