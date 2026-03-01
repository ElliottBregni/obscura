"""Delegation tool — enables agents to delegate tasks to local or remote peers.

Creates a ``task`` ToolSpec that resolves targets via the PeerRegistry
(local agents) or A2AClient (remote agents), runs the delegate in a
child session, and returns a structured summary.

Usage::

    from obscura.tools.delegation import DelegationContext, make_task_tool

    ctx = DelegationContext(
        peer_registry=runtime.peer_registry,
        event_store=store,
        can_delegate=True,
        delegate_allowlist=["researcher", "code-reviewer"],
        max_delegation_depth=3,
        current_depth=0,
    )
    tool_spec = make_task_tool(ctx)
    tool_registry.register(tool_spec)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from obscura.core.types import ToolSpec

if TYPE_CHECKING:
    from obscura.agent.peers import PeerRegistry
    from obscura.core.event_store import EventStoreProtocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DelegationContext:
    """Context required to build a delegation tool.

    Captures the caller's delegation policy and the infrastructure needed
    to resolve and invoke peers.
    """

    peer_registry: PeerRegistry | None = None
    event_store: EventStoreProtocol | None = None
    can_delegate: bool = False
    delegate_allowlist: list[str] = field(default_factory=lambda: list[str]())
    max_delegation_depth: int = 3
    current_depth: int = 0
    caller_agent_id: str = ""


def make_task_tool(ctx: DelegationContext) -> ToolSpec:
    """Build a ``task`` ToolSpec wired to the given delegation context.

    The returned tool:
    - Validates delegation is enabled (``can_delegate``)
    - Validates target is in ``delegate_allowlist`` (if non-empty)
    - Validates ``current_depth < max_delegation_depth``
    - Resolves target via PeerRegistry
    - Injects sub-agent constraints (run_shell only, rewrite hook, system prompt)
    - Runs the delegate via its ``run_loop()`` method
    - Creates a child session in the event store (if configured)
    - Returns a structured JSON result
    """

    async def _task_handler(prompt: str, target: str = "") -> str:
        # Gate: delegation enabled?
        if not ctx.can_delegate:
            return json.dumps({
                "ok": False,
                "error": "delegation_disabled",
                "message": "This agent is not configured for delegation.",
            })

        # Gate: depth limit
        if ctx.current_depth >= ctx.max_delegation_depth:
            return json.dumps({
                "ok": False,
                "error": "max_depth_exceeded",
                "message": (
                    f"Delegation depth {ctx.current_depth} "
                    f"exceeds max {ctx.max_delegation_depth}."
                ),
            })

        # Gate: target in allowlist (empty = all allowed)
        if ctx.delegate_allowlist and target not in ctx.delegate_allowlist:
            return json.dumps({
                "ok": False,
                "error": "target_not_allowed",
                "message": (
                    f"Target '{target}' not in allowlist: "
                    f"{ctx.delegate_allowlist}"
                ),
            })

        # Resolve target
        if ctx.peer_registry is None:
            return json.dumps({
                "ok": False,
                "error": "no_peer_registry",
                "message": "No peer registry configured for delegation.",
            })

        # Try resolving by name first (more user-friendly), then by ID
        agent = _resolve_by_name_or_id(ctx.peer_registry, target)
        if agent is None:
            return json.dumps({
                "ok": False,
                "error": "target_not_found",
                "message": f"Peer '{target}' not found in registry.",
            })

        # Create child session ID
        child_session_id = f"delegation-{uuid.uuid4().hex[:12]}"
        if ctx.event_store is not None:
            try:
                await ctx.event_store.create_session(
                    child_session_id,
                    f"delegate:{target}",
                )
            except Exception:
                logger.debug("Could not create child session", exc_info=True)

        # --- Inject sub-agent constraints BEFORE running ---
        # This ensures the child agent:
        #   1. Has a before(TOOL_CALL) hook that rewrites Claude Code native
        #      tool names (Glob, Grep, Read, ...) to run_shell equivalents.
        #   2. Has _tool_allowlist = ["run_shell"] so nothing else can slip
        #      through even if the rewrite hook misses something.
        #   3. Has SUBAGENT_SYSTEM_PROMPT prepended to its system prompt so
        #      the model knows it only has run_shell available.
        try:
            from obscura.tools.policy.models import inject_subagent_context
            inject_subagent_context(agent)
        except Exception:
            logger.warning(
                "inject_subagent_context failed for '%s' — proceeding without constraints",
                target,
                exc_info=True,
            )

        # Execute delegate
        try:
            result = await agent.run_loop(prompt)
            return json.dumps({
                "ok": True,
                "target": target,
                "session_id": child_session_id,
                "result": str(result),
            })
        except Exception as exc:
            logger.warning("Delegation to '%s' failed: %s", target, exc)
            return json.dumps({
                "ok": False,
                "error": "delegation_failed",
                "target": target,
                "session_id": child_session_id,
                "message": str(exc),
            })

    return ToolSpec(
        name="task",
        description=(
            "Delegate a task to another agent. "
            "Specify a prompt and an optional target agent name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task to delegate.",
                },
                "target": {
                    "type": "string",
                    "description": "Name or ID of the target agent.",
                },
            },
            "required": ["prompt"],
        },
        handler=_task_handler,
        required_tier="privileged",
    )


def _resolve_by_name_or_id(
    registry: PeerRegistry,
    target: str,
) -> Any:
    """Resolve a peer by name or agent ID.

    Tries agent_id first (exact match), then searches by name.
    Returns an Agent instance or None.
    """
    # Try direct ID resolution
    agent = registry.resolve(target)
    if agent is not None:
        return agent

    # Try by name
    refs = registry.discover()
    for ref in refs:
        if ref.name == target:
            return registry.resolve(ref)

    return None
