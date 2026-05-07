"""obscura.composition.blocks.repl_callbacks — REPL UI callback wiring.

Wires the REPL's interactive UI machinery (terminal widgets) into the
agent's tool callback slots so tools that ask the user questions, prompt
for permission, or render notifications hit the rich-text TUI.

Three of the four legacy callbacks are wired here:
- ask_user_callback        — questions to the user (with/without choices)
- plan_approval_callback   — exit-plan-mode confirmation
- user_interact_callback   — multi-modal: permission/notify/multi_select/question

The fourth (permission_mode_callback) stays inline in REPL because its
handler must mutate REPLContext.permission_mode, and REPLContext is
built AFTER session construction. Keeping it inline avoids the
chicken-and-egg.

The block sets the callbacks BOTH on the legacy module-level globals
(`UI.set_ask_user_callback` etc.) for tools that haven't migrated to
ToolContext yet, AND on `session.host_callbacks` for tools that read
via `current_tool_context()`. This duplication is deliberate during
the migration; remove once all tools are ToolContext-aware.

Reads:
    config.tools_enabled
    session.surface (REPL-only)

Writes:
    session.host_callbacks['ask_user_callback']
    session.host_callbacks['plan_approval_callback']
    session.host_callbacks['user_interact_callback']
    Module globals: UI.set_ask_user_callback, UI.set_user_interact_callback,
                     Session.set_plan_approval_callback

Resources: none

Opt-out:
    1. session.surface != "repl" → return immediately
    2. config.tools_enabled is False → return immediately

Replaces these legacy callsites (DELETED in same change):
    - obscura/cli/_repl_loop.py ask_user/plan_approval/user_interact wiring
    - obscura/cli/session.py:1314-1442 duplicate alt-path
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.composition.session import AgentSession, SessionConfig

logger = logging.getLogger(__name__)


async def install_repl_callbacks(
    session: AgentSession,
    config: SessionConfig,
) -> None:
    """Wire REPL TUI widgets to agent callbacks.

    See module docstring for full contract.
    """
    if session.surface != "repl":
        return
    if not config.tools_enabled:
        return

    try:
        from obscura.cli.widgets import (
            AttentionWidgetRequest,
            ModelQuestionRequest,
            MultiSelectRequest,
            NotifyWidgetRequest,
            PermissionWidgetRequest,
            ask_model_question,
            ask_multi_select,
            confirm_attention,
            confirm_permission,
            render_notification_banner,
        )
        from obscura.tools.system import Session, UI
    except Exception:
        logger.debug("install_repl_callbacks: widget imports failed", exc_info=True)
        return

    # ── ask_user_callback ─────────────────────────────────────────────
    async def _ask_user_handler(
        question: str,
        choices: list[str],
        allow_custom: bool = False,  # noqa: ARG001
    ) -> str:
        if choices:
            result = await confirm_attention(
                AttentionWidgetRequest(
                    request_id="ask_user",
                    agent_name="assistant",
                    message=question,
                    priority="normal",
                    actions=tuple(choices),
                ),
            )
            return result.action
        result = await ask_model_question(ModelQuestionRequest(question=question))
        return result.text

    try:
        UI.set_ask_user_callback(_ask_user_handler)
        session.host_callbacks["ask_user_callback"] = _ask_user_handler
    except Exception:
        logger.debug("install_repl_callbacks: ask_user wiring failed", exc_info=True)

    # ── plan_approval_callback ────────────────────────────────────────
    async def _plan_approval_handler(plan_summary: str) -> bool:
        result = await confirm_permission(
            PermissionWidgetRequest(
                action="Exit plan mode and begin implementation",
                reason=plan_summary or "Agent wants to leave plan mode.",
                risk="medium",
            ),
        )
        return result.action == "approve"

    try:
        Session.set_plan_approval_callback(_plan_approval_handler)
        session.host_callbacks["plan_approval_callback"] = _plan_approval_handler
        # Also wire directly onto ClaudeBackend so the SDK's can_use_tool hook
        # intercepts ExitPlanMode in REPL mode (not just via ToolContext).
        try:
            from obscura.providers.claude import ClaudeBackend as _CB

            _backend = getattr(session, "backend", None)
            if isinstance(_backend, _CB):
                _backend.set_plan_approval_callback(_plan_approval_handler)
                _pm_cb = session.host_callbacks.get("permission_mode_callback")
                if _pm_cb is not None:
                    _backend.set_permission_mode_callback(_pm_cb)
        except Exception:
            logger.debug(
                "install_repl_callbacks: ClaudeBackend plan_approval wiring failed",
                exc_info=True,
            )
    except Exception:
        logger.debug(
            "install_repl_callbacks: plan_approval wiring failed",
            exc_info=True,
        )

    # ── user_interact_callback ────────────────────────────────────────
    async def _user_interact_handler(**kwargs: Any) -> dict[str, Any]:
        mode = kwargs.get("mode", "question")

        if mode == "permission":
            result = await confirm_permission(
                PermissionWidgetRequest(
                    action=kwargs.get("action", ""),
                    reason=kwargs.get("reason", ""),
                    risk=kwargs.get("risk", "low"),
                ),
            )
            return {"approved": result.action == "approve"}

        if mode == "notify":
            render_notification_banner(
                NotifyWidgetRequest(
                    title=kwargs.get("title", ""),
                    message=kwargs.get("message", ""),
                    priority=kwargs.get("priority", "normal"),
                ),
            )
            return {}

        if mode == "multi_select":
            choices = kwargs.get("choices", [])
            question = kwargs.get("question", "")
            result = await ask_multi_select(
                MultiSelectRequest(
                    question=question,
                    choices=tuple(choices),
                ),
            )
            selected = [s.strip() for s in result.text.split(",") if s.strip()]
            return {"selected": selected}

        # Default: question mode
        choices = kwargs.get("choices", [])
        question = kwargs.get("question", "")
        if choices:
            result = await confirm_attention(
                AttentionWidgetRequest(
                    request_id="user_interact",
                    agent_name="assistant",
                    message=question,
                    priority="normal",
                    actions=tuple(choices),
                ),
            )
            return {"selected": result.action}
        result = await ask_model_question(ModelQuestionRequest(question=question))
        return {"selected": result.text}

    try:
        UI.set_user_interact_callback(_user_interact_handler)
        session.host_callbacks["user_interact_callback"] = _user_interact_handler
    except Exception:
        logger.debug(
            "install_repl_callbacks: user_interact wiring failed",
            exc_info=True,
        )

    logger.info(
        "install_repl_callbacks: wired ask_user + plan_approval + user_interact",
    )
