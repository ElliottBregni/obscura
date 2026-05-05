"""obscura.composition.a2a — `build_a2a_session` for A2A tasks.

Per-task session built when an A2A request arrives via JSON-RPC, REST,
SSE, gRPC, or Unix socket. Constructs an `AgentSession` with plugin
tools registered so A2A agents can ACTUALLY CALL TOOLS — previously
A2A agents returned placeholder strings because `get_runtime` was
None at server boot.

The on_input_required callback (built by the caller from
`A2AService._make_on_confirm`) is wired into `host_callbacks` so the
agent loop's tool confirmation gate parks the task in INPUT_REQUIRED
state.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from obscura.composition.blocks import install_plugin_tools
from obscura.composition.core import build_core_session
from obscura.composition.session import AgentSession, SessionConfig

if TYPE_CHECKING:
    from obscura.core.types import ToolCallInfo

logger = logging.getLogger(__name__)


async def build_a2a_session(
    config: SessionConfig,
    *,
    task_id: str,
    on_confirm: Callable[[ToolCallInfo], Awaitable[bool]] | None = None,
) -> AgentSession:
    """Build a session for one A2A task.

    Pipeline:
      core: ObscuraClient + backend.start() (with MCP servers from config)
      extras:
        1. install_plugin_tools  (SAME block as REPL/API — no drift)

    The `on_confirm` callback, if provided, is forwarded to
    `session.stream_loop(on_confirm=...)` by the caller — it's not
    threaded into `host_callbacks` because the agent loop accepts it
    as a direct kwarg.
    """
    extras: dict[str, Any] = dict(config.extras)
    extras["a2a_task_id"] = task_id
    config_with_task = SessionConfig(
        backend=config.backend,
        model=config.model,
        system_prompt=config.system_prompt,
        tools_enabled=config.tools_enabled,
        confirm_enabled=config.confirm_enabled,
        max_turns=config.max_turns,
        inject_claude_context=config.inject_claude_context,
        mcp_servers=list(config.mcp_servers),
        extras=extras,
    )

    session = await build_core_session(
        config_with_task,
        surface="a2a",
        user=None,
        session_id=task_id,  # use task id as session id for traceability
    )
    await install_plugin_tools(session, config_with_task)

    if on_confirm is not None:
        # Stash for the caller to forward to stream_loop.
        # We don't auto-thread into host_callbacks because A2A's confirm
        # bridge has different semantics (parks the task in INPUT_REQUIRED).
        session.host_callbacks["a2a_on_confirm"] = on_confirm

    return session
