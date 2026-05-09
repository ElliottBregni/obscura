"""obscura.core.agent_loop_factory — build a configured ``AgentLoopV2``.

The factory translates a flat kwarg surface (``hooks=...``,
``tool_allowlist=...``, ``capability_token=...``, ``on_confirm=...``,
``compiled_agent=...``, etc.) into the AgentLoopV2 middleware + hook
composition. Use this instead of constructing :class:`AgentLoopV2`
directly when you want the standard middleware stack (capability gate,
allowlist, confirmation, output overrides, compaction, predictive
cache, mid-stream retry).

.. code-block:: python

    from obscura.core.agent_loop_factory import make_agent_loop
    loop = make_agent_loop(backend, registry, hooks=hooks, capability_token=token)

Per-feature opt-outs (each defaults ON):

- ``OBSCURA_V2_PREDICTIVE_CACHE=0`` — disable speculative read-only
  prefetch
- ``OBSCURA_V2_SAFE_SKIP_CONFIRM=0`` — prompt for every tool, even
  read-only ones
- ``OBSCURA_V2_COMPILED_AGENT=0`` — ignore compiled_agent settings
  (instructions / max_iterations / allow / deny)
- ``OBSCURA_V2_RESUME_RETRY=0`` (or ``=safe``) — disable mid-stream
  retry, or downgrade to safe-mode (pre-first-chunk only)

Unrecognized kwargs drop with a one-time WARNING per process.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from obscura.core.tool_context import ToolContext

if TYPE_CHECKING:
    from obscura.core.agent_loop_v2 import AgentLoopV2
    from obscura.core.tools import ToolRegistry
    from obscura.core.types import BackendProtocol


logger = logging.getLogger(__name__)


__all__ = ["AgentLoopHandle", "make_agent_loop"]


# Alias kept for callers that historically imported the union name.
type AgentLoopHandle = "AgentLoopV2"


# Track which unsupported kwargs we've already warned about, to avoid
# spamming the log on every loop instantiation.
_warned_unsupported: set[str] = set()

# Kwargs the factory recognises and translates into middleware/hooks.
# Anything outside this set logs once as "ignored".
_SUPPORTED_KWARGS: frozenset[str] = frozenset(
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
        "host_callbacks",
        "compiled_agent",
        # Benign accepted-but-unused: ``auto_complete`` is always True
        # (only mode); ``backend_name`` was informational metadata;
        # ``turn_timeout_s`` should be wrapped via ``asyncio.timeout``
        # by the caller instead.
        "auto_complete",
        "backend_name",
        "turn_timeout_s",
    }
)


def _flag_enabled(env_name: str, default: str = "1") -> bool:
    """Read an OBSCURA_V2_* env var. Default is ON; set =0/false/no/off to disable."""
    raw = os.environ.get(env_name, default).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def make_agent_loop(
    backend: BackendProtocol,
    registry: ToolRegistry,
    **kwargs: Any,
) -> AgentLoopHandle:
    """Return an :class:`AgentLoopV2` configured from flat kwargs.

    Translates the kwarg surface into AgentLoopV2's middleware + hook
    composition. Unrecognized kwargs drop with a one-time WARNING.
    """
    return _build_loop(backend, registry, kwargs)


# ---------------------------------------------------------------------------
# Builder — translate flat kwargs to middleware + hooks
# ---------------------------------------------------------------------------


def _build_loop(
    backend: BackendProtocol,
    registry: ToolRegistry,
    kwargs: dict[str, Any],
) -> AgentLoopV2:
    # Wrap backend with retry-on-transient-error semantics. Default ON
    # (mid-stream resume) — set OBSCURA_V2_RESUME_RETRY=0 to disable
    # entirely, or =safe to use safe-mode (pre-first-chunk only).
    retry_mode = os.environ.get("OBSCURA_V2_RESUME_RETRY", "1").strip().lower()
    if retry_mode not in {"0", "false", "no", "off"}:
        from obscura.core.backend_retry import RetryingBackend

        # "safe" mode = pre-first-chunk only (no risk of dupes); anything
        # else (including "1"/"true"/"on") enables mid-stream resume,
        # relying on AgentLoopV2._seen_calls for tool_use_id dedup.
        allow_mid = retry_mode != "safe"
        backend = RetryingBackend(backend, allow_mid_stream=allow_mid)  # pyright: ignore[reportAssignmentType]

    from obscura.core.agent_loop_hooks import (
        compact_pre_turn,
        event_store_post_turn,
    )
    from obscura.core.agent_loop_middleware import (
        capability_gate,
        hook_middleware,
        tool_allowlist,
        tool_confirmation,
        tool_denylist,
        tool_output_level,
    )
    from obscura.core.agent_loop_v2 import AgentLoopV2, AgentLoopV2Config

    # ── compiled_agent translation (OBSCURA_V2_COMPILED_AGENT, default ON)
    # Pull settings from a CompiledAgent and merge into the kwargs dict
    # before the rest of the translation runs. Caller's explicit kwargs
    # win over compiled-agent values.
    compiled_agent = kwargs.get("compiled_agent")
    compiled_system_prompt = ""
    compiled_denylist: frozenset[str] | None = None
    if compiled_agent is not None and _flag_enabled("OBSCURA_V2_COMPILED_AGENT"):
        # Caller's explicit allowlist overrides the compiled one; same for
        # max_turns. Compiled values fill in only when caller didn't set.
        if kwargs.get("tool_allowlist") is None:
            ca_allow = getattr(compiled_agent, "tool_allowlist", None)
            if ca_allow is not None:
                kwargs["tool_allowlist"] = list(ca_allow)
        ca_deny = getattr(compiled_agent, "tool_denylist", None)
        if ca_deny:
            compiled_denylist = frozenset(ca_deny)
        if kwargs.get("max_turns") is None:
            ca_max = getattr(compiled_agent, "max_iterations", None)
            if ca_max is not None:
                kwargs["max_turns"] = int(ca_max)
        compiled_system_prompt = getattr(compiled_agent, "instructions", "") or ""

    # Warn once per unsupported kwarg.
    for k in kwargs:
        if k not in _SUPPORTED_KWARGS and k not in _warned_unsupported:
            _warned_unsupported.add(k)
            logger.warning(
                "make_agent_loop: kwarg %r is not recognised — ignored",
                k,
            )

    # ── Build the dispatch middleware list ─────────────────────────────────
    # Order matters: outer wrappers run first on entry, last on exit.
    # capability_gate goes outermost so denied calls never touch hooks /
    # confirmation. hook_middleware goes innermost so pre/post hooks see
    # the actual dispatch outcome.
    dispatch_middleware: list[Any] = []

    if "capability_token" in kwargs and kwargs["capability_token"] is not None:
        dispatch_middleware.append(capability_gate(kwargs["capability_token"]))

    if "tool_allowlist" in kwargs and kwargs["tool_allowlist"] is not None:
        dispatch_middleware.append(tool_allowlist(kwargs["tool_allowlist"]))

    # CompiledAgent's denylist (if any) — applied after allowlist so denied
    # names win over allowed ones.
    if compiled_denylist:
        dispatch_middleware.append(tool_denylist(compiled_denylist))

    if "on_confirm" in kwargs and kwargs["on_confirm"] is not None:
        # Pass the registry so the middleware can short-circuit prompts
        # for read-only tools (side_effects=="none" tools dispatch
        # silently). Set OBSCURA_V2_SAFE_SKIP_CONFIRM=0 to disable the
        # short circuit and prompt for every tool.
        skip_for_safe = _flag_enabled("OBSCURA_V2_SAFE_SKIP_CONFIRM")
        dispatch_middleware.append(
            tool_confirmation(
                kwargs["on_confirm"],
                registry=registry,
                skip_for_safe=skip_for_safe,
            )
        )

    if "tool_output_overrides" in kwargs or "tool_output_level" in kwargs:
        dispatch_middleware.append(
            tool_output_level(
                overrides=kwargs.get("tool_output_overrides"),
                default=kwargs.get("tool_output_level") or "standard",
            )
        )

    if "hooks" in kwargs and kwargs["hooks"] is not None:
        dispatch_middleware.append(hook_middleware(kwargs["hooks"]))

    # ── Predictive cache (OBSCURA_V2_PREDICTIVE_CACHE, default ON) ─────────
    # Speculatively dispatches read-only tool calls based on assistant
    # text deltas; on the actual tool_use the dispatch hits the cache.
    text_observers: list[Any] = []
    on_turn_start: Any = None
    if _flag_enabled("OBSCURA_V2_PREDICTIVE_CACHE"):
        from obscura.core.agent_loop_predictive import (
            V2PredictiveCache,
            make_predictive_observer,
            predictive_cache_middleware,
        )
        from obscura.runtime.predictive_tools import ToolPredictor

        pred_cache = V2PredictiveCache()
        pred_specs = {spec.name: spec for spec in registry.all()}
        predictor = ToolPredictor(tool_registry=pred_specs)
        dispatch_middleware.append(predictive_cache_middleware(pred_cache))

        # Stable observer object that closes over a mutable single-slot
        # holder for the current turn's ToolContext. on_turn_start
        # updates the slot; the observer reads the latest value on each
        # delta. List-of-one (rather than dict) keeps the slot strictly
        # typed and avoids the "mutate list passed to v2 constructor"
        # foot-gun.
        ctx_holder: list[ToolContext | None] = [None]

        async def _predictive_observer(delta: str) -> None:
            ctx = ctx_holder[0]
            if ctx is None:
                return
            obs = make_predictive_observer(
                predictor=predictor,
                cache=pred_cache,
                registry=registry,
                tool_ctx=ctx,
            )
            await obs(delta)

        text_observers.append(_predictive_observer)

        async def _start_predictive_turn(_turn: int, ctx: ToolContext) -> None:
            predictor.reset()
            pred_cache.clear()
            ctx_holder[0] = ctx

        on_turn_start = _start_predictive_turn

    # ── Build the pre_turn / post_turn hooks ───────────────────────────────
    pre_turn = None
    post_turn = None

    # Compaction is opt-in via context_budget + model_name. The hook
    # internally gates on the threshold, so calling it every turn is fine.
    if kwargs.get("context_budget") and kwargs.get("model_name"):
        pre_turn = compact_pre_turn(
            model_id=kwargs["model_name"],
            backend=backend,
        )

    # event_store + arbiter both go in post_turn. If both are present,
    # compose them.
    post_hooks: list[Any] = []
    if kwargs.get("event_store") is not None:
        post_hooks.append(
            event_store_post_turn(
                kwargs["event_store"],
                session_id=kwargs.get("agent_name", "default"),
            )
        )
    # arbiter is not exposed as a constructor kwarg — callers using
    # arbiter should construct AgentLoopV2 directly with the post_turn
    # hook. Leaving this as a documented extension point.

    if post_hooks:
        post_turn = _compose_post_turn_hooks(post_hooks)

    config = AgentLoopV2Config(
        max_turns=kwargs.get("max_turns", 10),
    )

    return AgentLoopV2(
        backend,
        registry,
        config=config,
        dispatch_middleware=dispatch_middleware or None,
        pre_turn=pre_turn,
        post_turn=post_turn,
        host_callbacks=kwargs.get("host_callbacks") or None,
        text_delta_observers=text_observers or None,
        on_turn_start=on_turn_start,
        system_prompt=compiled_system_prompt,
    )


def _compose_post_turn_hooks(hooks: list[Any]) -> Any:
    """Compose multiple post_turn hooks into one. Hooks fire in order;
    if any sets ``ctx.stop_after_turn``, later hooks still run (they
    shouldn't observe the kill flag, just react to results)."""

    async def composed(ctx: Any, result: Any) -> None:
        for h in hooks:
            await h(ctx, result)

    return composed
