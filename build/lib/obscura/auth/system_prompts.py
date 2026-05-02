"""obscura.auth.system_prompts -- Tier-specific system prompt templates.

Each capability tier receives a different system prompt that instructs
the model about its constraints, available tools, and behavioural policy.
The prompts are injected by the orchestrator (``ObscuraClient``) and are
**not** controllable via user input.

Prompt text lives in obscura/prompts/*.txt — edit those files, not this one.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.auth.capability import CapabilityTier

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load(name: str) -> str:
    """Load a prompt file by name (without .txt extension)."""
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        msg = f"Prompt file not found: {path}"
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Module-level constants — lazy-loaded from .txt files via __getattr__
# Preserve the existing public API for any direct imports.
# ---------------------------------------------------------------------------


def __getattr__(name: str) -> str:
    if name == "TIER_A_SYSTEM_PREFIX":
        return _load("tier_a_public")
    if name == "TIER_B_SYSTEM_PREFIX":
        return _load("tier_b_operator")
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_tier_system_prompt(
    tier: CapabilityTier,
    additional: str = "",
) -> str:
    """Build the complete system prompt for a given capability tier.

    Parameters
    ----------
    tier:
        The resolved capability tier.
    additional:
        Additional context-specific instructions to append (e.g. the
        caller's own system prompt).

    Returns
    -------
    str
        The full system prompt string.

    """
    # TODO: use tier_b_operator for PRIVILEGED once tier differentiation is enabled
    prefix = _load("tier_a_public")

    parts = [prefix.rstrip()]
    if additional:
        parts.append(f"\n## Additional Context\n{additional}")
    return "\n".join(parts)
