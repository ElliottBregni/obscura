"""obscura.core.agent_loop_factory — toggle between AgentLoop (v1) and AgentLoopV2.

Existing callers swap to v2 by changing one line:

.. code-block:: python

    # Before — direct v1 instantiation
    from obscura.core.agent_loop import AgentLoop
    loop = AgentLoop(backend, registry, hooks=hooks, capability_token=token, ...)

    # After — factory selects based on OBSCURA_AGENT_LOOP env
    from obscura.core.agent_loop_factory import make_agent_loop
    loop = make_agent_loop(backend, registry, hooks=hooks, capability_token=token, ...)

**v2 is the default.** Set ``OBSCURA_AGENT_LOOP=v1`` to revert to the
legacy loop while debugging. The factory translates v1's flat kwarg
surface into the v2 middleware composition automatically — capability
gates, hooks, allowlists, confirmation, output overrides, compaction,
predictive cache, compiled_agent, host_callbacks, and mid-stream retry
all map to the right middleware / hook entries.

Per-feature opt-outs (each defaults ON under v2):

- ``OBSCURA_V2_PREDICTIVE_CACHE=0`` — disable speculative read-only
  prefetch
- ``OBSCURA_V2_SAFE_SKIP_CONFIRM=0`` — prompt for every tool, even
  read-only ones
- ``OBSCURA_V2_COMPILED_AGENT=0`` — ignore compiled_agent settings
  (instructions / max_iterations / allow / deny)
- ``OBSCURA_V2_RESUME_RETRY=0`` (or ``=safe``) — disable mid-stream
  retry, or downgrade to safe-mode (pre-first-chunk only)

Unsupported v1 kwargs (rare — long tail like ``compiled_agent``-internal
fields, ``backend_name``, ``turn_timeout_s``) drop with a one-time
WARNING per process. v2 IS the production loop; these are footnotes.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.core.agent_loop_v2 import AgentLoopV2
    from obscura.core.tools import ToolRegistry
    from obscura.core.types import BackendProtocol


logger = logging.getLogger(__name__)


__all__ = ["AgentLoopHandle", "is_v2_enabled", "make_agent_loop"]


# v1 has been removed; the handle is now just AgentLoopV2. Kept as an
# alias for callers that imported the union name historically.
type AgentLoopHandle = "AgentLoopV2"


_V1_OPTOUT: frozenset[str] = frozenset({"v1", "0", "false", "no", "off"})

# One-time warn when caller explicitly asks for v1 (which is gone).
_warned_v1_optout: bool = False

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
        "host_callbacks",
        "compiled_agent",
        # Benign no-ops under v2 — accepted silently. ``auto_complete``
        # is always True in v2 (the only mode); ``backend_name`` was
        # informational metadata; ``turn_timeout_s`` should be wrapped
        # by the caller via asyncio.timeout instead.
        "auto_complete",
        "backend_name",
        "turn_timeout_s",
    }
)


def _flag_enabled(env_name: str, default: str = "1") -> bool:
    """Read an OBSCURA_V2_* env var. Default is ON; set =0/false/no/off to disable."""
    raw = os.environ.get(env_name, default).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def is_v2_enabled() -> bool:
    """Always True — v1 has been removed. Kept for backward compatibility
    with callers that previously gated behavior on the env var.

    The ``OBSCURA_AGENT_LOOP=v1`` opt-out still parses (we log a warning
    if it's set) but the function returns True regardless because there
    is no v1 to fall back to.
    """
    raw = os.environ.get("OBSCURA_AGENT_LOOP", "").strip().lower()
    if raw in _V1_OPTOUT:
        global _warned_v1_optout
        if not _warned_v1_optout:
            _warned_v1_optout = True
            logger.warning(
                "OBSCURA_AGENT_LOOP=%r requested v1 fallback, but v1 has "
                "been removed. Using v2 anyway. Unset the env var to "
                "silence this warning.",
                raw,
            )
    return True


def make_agent_loop(
    backend: BackendProtocol,
    registry: ToolRegistry,
    **v1_kwargs: Any,
) -> AgentLoopHandle:
    """Return an ``AgentLoopV2`` configured from v1-shaped kwargs.

    Translates v1's flat kwarg surface into the v2 middleware + hook
    composition. Unrecognized kwargs drop with a one-time WARNING. The
    function is named ``make_agent_loop`` (not ``make_agent_loop_v2``)
    so existing call sites that historically passed v1 kwargs continue
    to work without churn.
    """
    # Honor the v1 opt-out env var (logs warning, returns v2 anyway).
    is_v2_enabled()
    return _build_v2(backend, registry, v1_kwargs)


# ---------------------------------------------------------------------------
# v2 builder — translate v1 kwargs to middleware + hooks
# ---------------------------------------------------------------------------


def _build_v2(
    backend: BackendProtocol,
    registry: ToolRegistry,
    v1_kwargs: dict[str, Any],
) -> AgentLoopV2:
    # Wrap backend with retry-on-transient-error semantics. Default ON
    # (mid-stream resume) — set OBSCURA_V2_RESUME_RETRY=0 to disable
    # entirely, or =safe to use safe-mode (pre-first-chunk only).
    retry_mode = os.environ.get("OBSCURA_V2_RESUME_RETRY", "1").strip().lower()
    if retry_mode not in {"0", "false", "no", "off"}:
        from obscura.core.backend_retry import RetryingBackend

        # "safe" mode = pre-first-chunk only (no risk of dupes); anything
        # else (including "1"/"true"/"on"/"v2") enables mid-stream resume,
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
    # Pull settings from a CompiledAgent and merge into the v1-style kwargs
    # before the rest of the translation runs. Caller's explicit kwargs win
    # over compiled-agent values.
    compiled_agent = v1_kwargs.get("compiled_agent")
    compiled_system_prompt = ""
    compiled_denylist: frozenset[str] | None = None
    if compiled_agent is not None and _flag_enabled("OBSCURA_V2_COMPILED_AGENT"):
        # Caller's explicit allowlist overrides the compiled one; same for
        # max_turns. Compiled values fill in only when caller didn't set.
        if v1_kwargs.get("tool_allowlist") is None:
            ca_allow = getattr(compiled_agent, "tool_allowlist", None)
            if ca_allow is not None:
                v1_kwargs["tool_allowlist"] = list(ca_allow)
        ca_deny = getattr(compiled_agent, "tool_denylist", None)
        if ca_deny:
            compiled_denylist = frozenset(ca_deny)
        if v1_kwargs.get("max_turns") is None:
            ca_max = getattr(compiled_agent, "max_iterations", None)
            if ca_max is not None:
                v1_kwargs["max_turns"] = int(ca_max)
        compiled_system_prompt = getattr(compiled_agent, "instructions", "") or ""

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

    # CompiledAgent's denylist (if any) — applied after allowlist so denied
    # names win over allowed ones.
    if compiled_denylist:
        dispatch_middleware.append(tool_denylist(compiled_denylist))

    if "on_confirm" in v1_kwargs and v1_kwargs["on_confirm"] is not None:
        # Pass the registry so the middleware can short-circuit prompts
        # for read-only tools (v1 parity — side_effects=="none" tools
        # dispatch silently). Set OBSCURA_V2_SAFE_SKIP_CONFIRM=0 to
        # disable the short circuit and prompt for every tool.
        skip_for_safe = _flag_enabled("OBSCURA_V2_SAFE_SKIP_CONFIRM")
        dispatch_middleware.append(
            tool_confirmation(
                v1_kwargs["on_confirm"],
                registry=registry,
                skip_for_safe=skip_for_safe,
            )
        )

    if "tool_output_overrides" in v1_kwargs or "tool_output_level" in v1_kwargs:
        dispatch_middleware.append(
            tool_output_level(
                overrides=v1_kwargs.get("tool_output_overrides"),
                default=v1_kwargs.get("tool_output_level") or "standard",
            )
        )

    if "hooks" in v1_kwargs and v1_kwargs["hooks"] is not None:
        dispatch_middleware.append(hook_middleware(v1_kwargs["hooks"]))

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

        # Stable observer object that closes over a mutable holder for
        # the current turn's ToolContext. on_turn_start updates the
        # holder; the observer reads the latest value on each delta.
        # This avoids the "mutate list passed to v2 constructor" foot-gun.
        ctx_holder: dict[str, Any] = {"ctx": None}

        async def _predictive_observer(delta: str) -> None:
            ctx = ctx_holder.get("ctx")
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

        async def _start_predictive_turn(_turn: int, ctx: Any) -> None:
            predictor.reset()
            pred_cache.clear()
            ctx_holder["ctx"] = ctx

        on_turn_start = _start_predictive_turn

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
        host_callbacks=v1_kwargs.get("host_callbacks") or None,
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
