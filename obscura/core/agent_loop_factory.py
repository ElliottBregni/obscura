"""obscura.core.agent_loop_factory — toggle between AgentLoop (v1) and AgentLoopV2.

Existing callers can swap to v2 by changing one line:

.. code-block:: python

    # Before — direct v1 instantiation
    from obscura.core.agent_loop import AgentLoop
    loop = AgentLoop(backend, registry, hooks=hooks, capability_token=token, ...)

    # After — factory selects based on OBSCURA_AGENT_LOOP env
    from obscura.core.agent_loop_factory import make_agent_loop
    loop = make_agent_loop(backend, registry, hooks=hooks, capability_token=token, ...)

Set ``OBSCURA_AGENT_LOOP=v2`` to opt into v2; default is ``v1`` until eval
data confirms parity. The factory translates v1's flat kwarg surface into
the v2 middleware composition automatically — capability gates, hooks,
allowlists, confirmation, output overrides, and compaction all map to the
right middleware / hook entries.

Caveats — features that don't yet have a v2 equivalent (predictive cache,
intra-turn stream retry with seen_calls, host_callbacks plumbing) are
**ignored** when the toggle is v2 with a one-time WARNING log per process.
That's the explicit incremental-port story: v2 is fully usable for the
features it supports, and unsupported kwargs degrade gracefully rather
than silently misleading the caller.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.core.agent_loop import AgentLoop
    from obscura.core.agent_loop_v2 import AgentLoopV2
    from obscura.core.tools import ToolRegistry
    from obscura.core.types import BackendProtocol


logger = logging.getLogger(__name__)


__all__ = ["AgentLoopHandle", "is_v2_enabled", "make_agent_loop"]


# A union of the two loop types. The factory's return type is the union;
# callers iterate ``run()`` the same way on both.
AgentLoopHandle = "AgentLoop | AgentLoopV2"


_TRUTHY: frozenset[str] = frozenset({"v2", "1", "true", "yes", "on", "y", "t"})

# Track which unsupported v1 kwargs we've already warned about, to avoid
# spamming the log on every loop instantiation.
_warned_unsupported: set[str] = set()

# v1 kwargs that v2 supports (via middleware/hooks). Anything outside this
# set is logged as "ignored under v2".
_V2_SUPPORTED_V1_KWARGS: frozenset[str] = frozenset(
    {
        "max_turns",
        "hooks",
        "capability_token",
        "tool_allowlist",
        "on_confirm",
        "tool_output_level",
        "tool_output_overrides",
        "event_store",
        "agent_name",
        "model_name",
        "context_budget",
    }
)


def is_v2_enabled() -> bool:
    """True when ``OBSCURA_AGENT_LOOP=v2`` (or any truthy synonym).

    Default False. Set this in your shell or your runner config to opt in.
    """
    raw = os.environ.get("OBSCURA_AGENT_LOOP", "").strip().lower()
    return raw in _TRUTHY


def make_agent_loop(
    backend: BackendProtocol,
    registry: ToolRegistry,
    **v1_kwargs: Any,
) -> AgentLoopHandle:
    """Return either an ``AgentLoop`` (v1) or an ``AgentLoopV2`` based on env.

    The returned object exposes ``.run(prompt, ...)`` returning an async
    iterator of :class:`AgentEvent` — the same interface on both.

    All keyword arguments are the v1 ``AgentLoop`` signature. When v2 is
    selected, this function translates them into v2 middleware + hooks.
    Unrecognized / unsupported kwargs are dropped with a one-time WARNING.
    """
    if is_v2_enabled():
        return _build_v2(backend, registry, v1_kwargs)
    return _build_v1(backend, registry, v1_kwargs)


# ---------------------------------------------------------------------------
# v1 builder — straight passthrough
# ---------------------------------------------------------------------------


def _build_v1(
    backend: BackendProtocol,
    registry: ToolRegistry,
    kwargs: dict[str, Any],
) -> AgentLoop:
    from obscura.core.agent_loop import AgentLoop

    return AgentLoop(backend, registry, **kwargs)


# ---------------------------------------------------------------------------
# v2 builder — translate v1 kwargs to middleware + hooks
# ---------------------------------------------------------------------------


def _build_v2(
    backend: BackendProtocol,
    registry: ToolRegistry,
    v1_kwargs: dict[str, Any],
) -> AgentLoopV2:
    from obscura.core.agent_loop_hooks import (
        compact_pre_turn,
        event_store_post_turn,
    )
    from obscura.core.agent_loop_middleware import (
        capability_gate,
        hook_middleware,
        tool_allowlist,
        tool_confirmation,
        tool_output_level,
    )
    from obscura.core.agent_loop_v2 import AgentLoopV2, AgentLoopV2Config

    # Warn once per unsupported kwarg.
    for k in v1_kwargs:
        if k not in _V2_SUPPORTED_V1_KWARGS and k not in _warned_unsupported:
            _warned_unsupported.add(k)
            logger.warning(
                "make_agent_loop: v1 kwarg %r has no v2 equivalent yet — ignored "
                "under OBSCURA_AGENT_LOOP=v2 (set =v1 to keep using it)",
                k,
            )

    # ── Build the dispatch middleware list ─────────────────────────────────
    # Order matters: outer wrappers run first on entry, last on exit.
    # capability_gate goes outermost so denied calls never touch hooks /
    # confirmation. hook_middleware goes innermost so pre/post hooks see
    # the actual dispatch outcome.
    dispatch_middleware: list[Any] = []

    if "capability_token" in v1_kwargs and v1_kwargs["capability_token"] is not None:
        dispatch_middleware.append(capability_gate(v1_kwargs["capability_token"]))

    if "tool_allowlist" in v1_kwargs and v1_kwargs["tool_allowlist"] is not None:
        dispatch_middleware.append(tool_allowlist(v1_kwargs["tool_allowlist"]))

    if "on_confirm" in v1_kwargs and v1_kwargs["on_confirm"] is not None:
        dispatch_middleware.append(tool_confirmation(v1_kwargs["on_confirm"]))

    if "tool_output_overrides" in v1_kwargs or "tool_output_level" in v1_kwargs:
        dispatch_middleware.append(
            tool_output_level(
                overrides=v1_kwargs.get("tool_output_overrides"),
                default=v1_kwargs.get("tool_output_level") or "standard",
            )
        )

    if "hooks" in v1_kwargs and v1_kwargs["hooks"] is not None:
        dispatch_middleware.append(hook_middleware(v1_kwargs["hooks"]))

    # ── Build the pre_turn / post_turn hooks ───────────────────────────────
    pre_turn = None
    post_turn = None

    # Compaction is opt-in via context_budget + model_name. v1 used inline
    # threshold logic; v2 just calls compact_history each turn (which itself
    # gates on the threshold internally).
    if v1_kwargs.get("context_budget") and v1_kwargs.get("model_name"):
        pre_turn = compact_pre_turn(model_id=v1_kwargs["model_name"])

    # event_store + arbiter both go in post_turn. If both are present,
    # compose them.
    post_hooks: list[Any] = []
    if v1_kwargs.get("event_store") is not None:
        post_hooks.append(
            event_store_post_turn(
                v1_kwargs["event_store"],
                session_id=v1_kwargs.get("agent_name", "default"),
            )
        )
    # v1 didn't expose ``arbiter`` as a constructor kwarg directly; it was
    # set via ``self._arbiter_killed`` flags by external code. v2's
    # adapter doesn't need to wire arbiter here — callers using arbiter
    # should construct AgentLoopV2 directly with the post_turn hook.
    # Leaving this as a documented extension point.

    if post_hooks:
        post_turn = _compose_post_turn_hooks(post_hooks)

    config = AgentLoopV2Config(
        max_turns=v1_kwargs.get("max_turns", 10),
    )

    return AgentLoopV2(
        backend,
        registry,
        config=config,
        dispatch_middleware=dispatch_middleware or None,
        pre_turn=pre_turn,
        post_turn=post_turn,
    )


def _compose_post_turn_hooks(hooks: list[Any]) -> Any:
    """Compose multiple post_turn hooks into one. Hooks fire in order;
    if any sets ``ctx.stop_after_turn``, later hooks still run (they
    shouldn't observe the kill flag, just react to results)."""

    async def composed(ctx: Any, result: Any) -> None:
        for h in hooks:
            await h(ctx, result)

    return composed
