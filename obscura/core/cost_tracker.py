"""obscura.core.cost_tracker — Per-session token and cost tracking.

Tracks input/output tokens per model turn and computes USD cost using
pricing from the model registry.  Wire into the agent loop via an
after-hook on ``AgentEventKind.DONE``.

Usage::

    tracker = CostTracker()
    tracker.record(input_tokens=1500, output_tokens=300, model="claude-sonnet-4-5")
    print(tracker.session_total_usd())   # e.g. 0.0045
    print(tracker.summary())             # human-readable breakdown
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Default pricing per 1K tokens (USD).  Override via set_pricing().
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # (input_per_1k, output_per_1k)
    # Anthropic
    "claude-opus-4-5": (0.015, 0.075),
    "claude-sonnet-4-5": (0.003, 0.015),
    "claude-haiku-3-5": (0.0008, 0.004),
    "claude-opus-4": (0.015, 0.075),
    "claude-sonnet-4": (0.003, 0.015),
    # OpenAI
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    "o1": (0.015, 0.060),
    "o3": (0.010, 0.040),
    "o3-mini": (0.0011, 0.0044),
    # Fallback
    "default": (0.003, 0.015),
}


@dataclass
class TurnCost:
    """Cost record for a single model turn."""

    input_tokens: int
    output_tokens: int
    model: str
    cost_usd: float
    timestamp: float = field(default_factory=time.time)


class CostTracker:
    """Tracks token usage and computes session cost."""

    def __init__(self, pricing: dict[str, tuple[float, float]] | None = None) -> None:
        self._pricing = pricing or dict(_DEFAULT_PRICING)
        self._turns: list[TurnCost] = []

    def set_pricing(
        self,
        model: str,
        input_per_1k: float,
        output_per_1k: float,
    ) -> None:
        """Override pricing for a specific model."""
        self._pricing[model] = (input_per_1k, output_per_1k)

    def _get_pricing(self, model: str) -> tuple[float, float]:
        """Resolve pricing for a model (exact match → prefix match → default)."""
        if model in self._pricing:
            return self._pricing[model]
        ml = model.lower()
        for key, val in self._pricing.items():
            if key == "default":
                continue
            if ml.startswith(key.lower()) or key.lower().startswith(ml):
                return val
        return self._pricing.get("default", (0.003, 0.015))

    def record(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str,
    ) -> TurnCost:
        """Record a model turn and compute its cost."""
        inp_rate, out_rate = self._get_pricing(model)
        cost = (input_tokens / 1000.0) * inp_rate + (output_tokens / 1000.0) * out_rate
        turn = TurnCost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            cost_usd=cost,
        )
        self._turns.append(turn)
        return turn

    def session_total_usd(self) -> float:
        """Total session cost in USD."""
        return sum(t.cost_usd for t in self._turns)

    def total_input_tokens(self) -> int:
        return sum(t.input_tokens for t in self._turns)

    def total_output_tokens(self) -> int:
        return sum(t.output_tokens for t in self._turns)

    def turn_count(self) -> int:
        return len(self._turns)

    def breakdown(self) -> list[dict[str, Any]]:
        """Return per-turn breakdown as list of dicts."""
        return [
            {
                "turn": i + 1,
                "model": t.model,
                "input_tokens": t.input_tokens,
                "output_tokens": t.output_tokens,
                "cost_usd": round(t.cost_usd, 6),
            }
            for i, t in enumerate(self._turns)
        ]

    def summary(self) -> str:
        """Human-readable cost summary."""
        total = self.session_total_usd()
        inp = self.total_input_tokens()
        out = self.total_output_tokens()
        turns = self.turn_count()
        return (
            f"Session: {turns} turns, "
            f"{inp:,} input + {out:,} output tokens, "
            f"${total:.4f} USD"
        )

    def reset(self) -> None:
        """Clear all recorded turns."""
        self._turns.clear()


# Module-level singleton.
_tracker: CostTracker | None = None


def get_cost_tracker() -> CostTracker:
    """Return the global ``CostTracker`` singleton."""
    global _tracker
    if _tracker is None:
        _tracker = CostTracker()
    return _tracker
