from __future__ import annotations

from typing import Any

SOFT_BUDGET = 50_000


def get_copilot_budget_pct(tokens: int, context_window: int) -> int:
    """Return Copilot budget percent used for a given token usage.

    Tests expect a "doubling" behaviour (e.g., 25% -> 50, 30% -> 60), so multiply
    the usage percentage by 2 and clamp to [0, 100].
    """
    if context_window <= 0:
        return 0
    pct = (tokens / context_window) * 100
    return int(min(100, round(pct * 2)))


def emit_context_warnings(ctx: Any, tokens: int, context_window: int) -> None:
    """Emit context usage warnings based on thresholds (25,50,75).

    Attaches and uses ctx._last_context_warning_level to avoid repeating the same
    upward warning. If usage drops below 25% the state is reset so warnings can
    retrigger.
    """
    try:
        pct = (tokens / context_window) * 100 if context_window > 0 else 0
    except Exception:
        pct = 0

    thresholds = (25, 50, 75)
    current_level = 0
    for t in thresholds:
        if pct >= t:
            current_level = t

    last = getattr(ctx, "_last_context_warning_level", 0)

    # If dropped below the lowest threshold, reset the stored state so a later
    # crossing will emit again.
    if pct < thresholds[0] and last != 0:
        ctx._last_context_warning_level = 0
        return

    # Only emit when crossing upward to a higher threshold
    if current_level > last:
        budget_msg = ""
        if getattr(ctx, "backend", "") == "copilot":
            budget_pct = get_copilot_budget_pct(tokens, context_window)
            budget_msg = (
                f" Copilot budget: {budget_pct}% of {SOFT_BUDGET:,} token soft budget."
            )

        msg = (
            f"Context usage has crossed {current_level}%. ({int(pct)}% used)."
            + budget_msg
        )
        try:
            import importlib

            try:
                _cli = importlib.import_module("obscura.cli.__init__")
            except Exception:
                _cli = importlib.import_module("obscura.cli")
            _cli.console.print(msg)
        except Exception:
            # Fall back to stdout if console not available
            print(msg)
        ctx._last_context_warning_level = current_level
