"""obscura.core.agent_loop_middleware — Composable middleware for AgentLoopV2.

Each function returns a :class:`DispatchMiddleware` — a callable that wraps
the per-node executor. Middleware are applied outermost-first by
:class:`AgentLoopV2`: the first item in ``dispatch_middleware`` runs first
on the way in and last on the way out.

These cover the v1 ``AgentLoop`` features that were inlined into
``_execute_single_tool``. Porting v1 → v2 is a matter of constructing the
right middleware list:

============================  ===================================
v1 ``AgentLoop`` kwarg        v2 middleware
============================  ===================================
``capability_token=...``      :func:`capability_gate`
``tool_allowlist=[...]``      :func:`tool_allowlist`
``hooks=...``                 :func:`hook_middleware`
``on_confirm=...``            :func:`tool_confirmation`
``tool_output_overrides=...`` :func:`tool_output_level`
============================  ===================================

All middleware here are **optional** — pass only the ones you need.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.core.types import ContentBlock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from obscura.core.dag import DAGNode
    from obscura.core.hooks import HookRegistry
    from obscura.core.tools import ToolRegistry


logger = logging.getLogger(__name__)


__all__ = [
    "capability_gate",
    "hook_middleware",
    "tool_allowlist",
    "tool_confirmation",
    "tool_denylist",
    "tool_output_level",
]


# Type alias — narrowed for readability. Matches Scheduler's NodeExecutor.
_NodeExecutor = "Callable[[DAGNode, dict[str, Any]], Awaitable[list[ContentBlock]]]"


# ---------------------------------------------------------------------------
# capability_gate
# ---------------------------------------------------------------------------


def capability_gate(
    token: Any,  # CapabilityToken — typed loosely to keep this file independent
    *,
    is_allowed: Callable[[Any, str], bool] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Reject dispatches when the token doesn't grant access to the tool.

    *token* is opaque to the middleware. Pass *is_allowed* to override the
    default check, which uses ``token.allows(tool_name)`` if the method
    exists, else accepts everything (no-op gate). The override exists so
    callers can plug in the v1 ``CapabilityToken.is_authorized()`` style
    check without depending on the v1 type here.
    """

    def _default_check(tok: Any, name: str) -> bool:
        method = getattr(tok, "allows", None)
        if callable(method):
            return bool(method(name))
        method = getattr(tok, "is_authorized", None)
        if callable(method):
            return bool(method(name))
        # Token has no allows/is_authorized — treat as fully permissive
        # (caller probably passed something they meant to use elsewhere).
        return True

    check = is_allowed or _default_check

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            if not check(token, node.tool_name):
                logger.info(
                    "capability_gate: denied %s (capability not granted)",
                    node.tool_name,
                )
                # Return a tool_result-shaped block so the aggregator's
                # is_error detection picks it up (TextBlock doesn't carry
                # is_error; ToolResultBlock does).
                return [
                    ContentBlock(
                        kind="tool_result",
                        tool_use_id=node.tool_use_id,
                        text=(
                            f"Capability denied: tool '{node.tool_name}' "
                            "is not authorized for this token."
                        ),
                        is_error=True,
                    ),
                ]
            return await inner(node, resolved)

        return wrapped

    return wrap


# ---------------------------------------------------------------------------
# tool_allowlist
# ---------------------------------------------------------------------------


def tool_allowlist(
    allowed: list[str] | set[str] | tuple[str, ...],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Reject dispatches for tool names not in *allowed*.

    Equivalent to v1's ``AgentLoop(tool_allowlist=...)``. Pass an empty
    collection to deny everything; pass ``None`` (don't include this
    middleware) to allow everything.
    """
    allowed_set = frozenset(allowed)

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            if node.tool_name not in allowed_set:
                logger.info(
                    "tool_allowlist: denied %s (not in allowlist)", node.tool_name
                )
                return [
                    ContentBlock(
                        kind="tool_result",
                        tool_use_id=node.tool_use_id,
                        text=f"Tool '{node.tool_name}' is not in the allowlist.",
                        is_error=True,
                    ),
                ]
            return await inner(node, resolved)

        return wrapped

    return wrap


# ---------------------------------------------------------------------------
# tool_denylist
# ---------------------------------------------------------------------------


def tool_denylist(
    denied: list[str] | set[str] | tuple[str, ...] | frozenset[str],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Reject dispatches for tool names IN *denied*.

    Inverse of :func:`tool_allowlist`. CompiledAgent uses both: the
    workspace declares an allowlist (only-these) AND a denylist
    (never-these). Apply both middleware in series — the workspace
    can use either or both depending on its policy.
    """
    denied_set = frozenset(denied)

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            if node.tool_name in denied_set:
                logger.info("tool_denylist: denied %s (in denylist)", node.tool_name)
                return [
                    ContentBlock(
                        kind="tool_result",
                        tool_use_id=node.tool_use_id,
                        text=f"Tool '{node.tool_name}' is in the denylist.",
                        is_error=True,
                    ),
                ]
            return await inner(node, resolved)

        return wrapped

    return wrap


# ---------------------------------------------------------------------------
# hook_middleware
# ---------------------------------------------------------------------------


def hook_middleware(
    hooks: HookRegistry,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Fire pre/post tool-use hooks around dispatch.

    Mirrors v1's per-tool hook firing in ``_execute_single_tool``. Hooks
    that raise are logged and **swallowed** — a hook bug shouldn't break
    the agent. (v1 has the same swallow semantic.)
    """

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            await _safe_run_hook(hooks, "before", node)
            result = await inner(node, resolved)
            await _safe_run_hook(hooks, "after", node)
            return result

        return wrapped

    return wrap


async def _safe_run_hook(
    hooks: HookRegistry, phase: str, node: DAGNode
) -> None:
    """Build a TOOL_CALL AgentEvent for *node* and dispatch it through the
    registry's before/after pipeline. Per-hook exceptions are already
    logged inside ``HookRegistry``; the outer try only catches registry-
    level failures so a misconfigured registry can't break the loop.
    """
    from obscura.core.enums.agent import AgentEventKind
    from obscura.core.types import AgentEvent

    event = AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        tool_name=node.tool_name,
        tool_input=node.tool_input,
        tool_use_id=node.tool_use_id,
    )
    try:
        if phase == "before":
            await hooks.run_before(event)
        else:
            await hooks.run_after(event)
    except Exception:
        logger.exception(
            "%s hook dispatch failed for tool %r — swallowing",
            phase,
            node.tool_name,
        )


# ---------------------------------------------------------------------------
# tool_confirmation
# ---------------------------------------------------------------------------


def tool_confirmation(
    on_confirm: Callable[[DAGNode], bool] | Callable[[DAGNode], Awaitable[bool]],
    *,
    registry: ToolRegistry | None = None,
    skip_for_safe: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Ask *on_confirm* before dispatching each tool. Reject if it returns False.

    Mirrors v1's ``AgentLoop(on_confirm=...)`` callback. *on_confirm* may
    be sync or async; if async, it's awaited.

    When *registry* is provided AND *skip_for_safe* is True (both default),
    tools whose ``ToolSpec.side_effects == "none"`` skip the confirmation
    prompt entirely — matching v1's read-only-tools-pass-through behavior.
    Without the registry the middleware can't read ``side_effects`` and
    falls back to prompting for every node.

    Pass ``skip_for_safe=False`` to prompt for every tool unconditionally.
    """

    def _is_safe(tool_name: str) -> bool:
        """True if the tool's side_effects is "none". False if unknown."""
        if registry is None:
            return False
        spec = registry.get(tool_name)
        if spec is None:
            return False
        return getattr(spec, "side_effects", "") == "none"

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            # Safe-tool short circuit: skip prompt for known read-only tools.
            if skip_for_safe and _is_safe(node.tool_name):
                return await inner(node, resolved)

            decision = on_confirm(node)
            if hasattr(decision, "__await__"):
                decision = await decision  # type: ignore[assignment]
            if not decision:
                logger.info(
                    "tool_confirmation: denied %s (user rejected)", node.tool_name
                )
                return [
                    ContentBlock(
                        kind="tool_result",
                        tool_use_id=node.tool_use_id,
                        text="Tool call denied by user",
                        is_error=True,
                    ),
                ]
            return await inner(node, resolved)

        return wrapped

    return wrap


# ---------------------------------------------------------------------------
# tool_output_level
# ---------------------------------------------------------------------------


def tool_output_level(
    overrides: dict[str, str] | None = None,
    *,
    default: str = "standard",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Filter / level-shift tool output post-dispatch.

    Mirrors v1's ``tool_output_level`` + ``tool_output_overrides``. When
    a tool's effective level is ``"silent"``, the result is replaced with
    a single empty text block (still satisfies the SDK contract — the
    tool_use gets a tool_result, just an empty one). ``"compact"`` /
    ``"standard"`` / ``"verbose"`` are passthroughs in v2 today; they're
    a forward-compatibility shim until per-tool output formatters land.

    *overrides* is the per-tool map (``{tool_name: level}``); *default*
    is the level for tools not in the map.
    """
    table = dict(overrides or {})

    def _level_for(name: str) -> str:
        return table.get(name, default)

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            blocks = await inner(node, resolved)
            level = _level_for(node.tool_name)
            if level == "silent":
                # Replace content with an empty text block — the SDK
                # contract still gets a matching tool_result.
                return [ContentBlock(kind="text", text="")]
            return blocks

        return wrapped

    return wrap
