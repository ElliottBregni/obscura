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


logger = logging.getLogger(__name__)


__all__ = [
    "capability_gate",
    "hook_middleware",
    "tool_allowlist",
    "tool_confirmation",
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
                return [
                    ContentBlock(
                        kind="text",
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
                        kind="text",
                        text=f"Tool '{node.tool_name}' is not in the allowlist.",
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
    """Fire ``pre_tool_use`` and ``post_tool_use`` hooks around dispatch.

    Mirrors v1's per-tool hook firing in ``_execute_single_tool``. Hooks
    that raise are logged and **swallowed** — a hook bug shouldn't break
    the agent. (v1 has the same swallow semantic.)
    """

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            await _safe_run_hook(hooks, "pre_tool_use", node)
            result = await inner(node, resolved)
            await _safe_run_hook(hooks, "post_tool_use", node, result)
            return result

        return wrapped

    return wrap


async def _safe_run_hook(hooks: HookRegistry, name: str, *args: Any) -> None:
    """Invoke a hook by name, swallowing exceptions."""
    runner = getattr(hooks, "run", None) or getattr(hooks, "fire", None)
    if runner is None:
        return
    try:
        result = runner(name, *args)
        if hasattr(result, "__await__"):
            await result
    except Exception:
        logger.exception(
            "hook %r raised — swallowing (hooks must not break loop)", name
        )


# ---------------------------------------------------------------------------
# tool_confirmation
# ---------------------------------------------------------------------------


def tool_confirmation(
    on_confirm: Callable[[DAGNode], bool] | Callable[[DAGNode], Awaitable[bool]],
    *,
    skip_for_safe: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Ask *on_confirm* before dispatching each tool. Reject if it returns False.

    Mirrors v1's ``AgentLoop(on_confirm=...)`` callback. *on_confirm* may
    be sync or async; if async, it's awaited.

    When *skip_for_safe* (default), tools whose ``ToolSpec.side_effects``
    is ``"none"`` skip the confirmation prompt — the v1 default for
    read-only tools. To prompt for everything, pass ``skip_for_safe=False``.

    Note: ``side_effects`` is a property of the tool spec, not the node.
    The middleware can't read it without registry access; we use a
    heuristic on the node's tool name. For full v1 parity, pass a
    custom check via ``on_confirm`` that consults the registry directly.
    """

    def wrap(inner: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            node: DAGNode,
            resolved: dict[str, Any],
        ) -> list[ContentBlock]:
            decision = on_confirm(node)
            if hasattr(decision, "__await__"):
                decision = await decision  # type: ignore[assignment]
            if not decision:
                logger.info(
                    "tool_confirmation: denied %s (user rejected)", node.tool_name
                )
                return [
                    ContentBlock(
                        kind="text",
                        text="Tool call denied by user",
                        is_error=True,
                    ),
                ]
            return await inner(node, resolved)

        # ``skip_for_safe`` is reserved for future use — when v2 surfaces
        # ToolSpec to the middleware via ``resolved``, this can short-circuit
        # safe tools without prompting. Today the prompt fires for every node.
        _ = skip_for_safe  # mark used for static checkers
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
