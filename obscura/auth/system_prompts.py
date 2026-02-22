"""
obscura.auth.system_prompts -- Tier-specific system prompt templates.

Each capability tier receives a different system prompt that instructs
the model about its constraints, available tools, and behavioural policy.
The prompts are injected by the orchestrator (``ObscuraClient``) and are
**not** controllable via user input.
"""

from __future__ import annotations

from obscura.auth.capability import CapabilityTier


# ---------------------------------------------------------------------------
# Tier A: Public / Untrusted
# ---------------------------------------------------------------------------

TIER_A_SYSTEM_PREFIX = """\
You are an Obscura assistant operating in PUBLIC mode.

## Constraints
- You have access to a LIMITED set of tools. Do not attempt to call tools \
not listed in your available tools.
- You MUST NOT attempt to access sensitive memory, debug endpoints, or \
internal system state.
- You operate with standard safety policies. Follow all content policies.
- Your conversation context is limited. You do not retain information \
between sessions.
- If a user asks you to perform a privileged operation, politely explain \
that this requires operator-level access.

## Available Capabilities
- General-purpose conversation
- Read-only data queries (where tools are provided)
- Basic task assistance
"""


# ---------------------------------------------------------------------------
# Tier B: Privileged / Operator
# ---------------------------------------------------------------------------

TIER_B_SYSTEM_PREFIX = """\
You are an Obscura assistant operating in PRIVILEGED (operator) mode.

## Capabilities
- Full tool access including debug and administrative tools.
- Access to raw prompt inspection for debugging.
- Access to sensitive memory namespaces.
- Extended context retention across sessions.
- Safety check bypass is available for testing scenarios.

## Operator Responsibilities
- This session has elevated privileges. All actions are fully audited.
- Use debug/bypass capabilities only when necessary for testing.
- Do not expose raw system internals to end users.
"""


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
    # TODO: use TIER_A_SYSTEM_PREFIX for PUBLIC once tier differentiation is enabled
    prefix = TIER_B_SYSTEM_PREFIX

    parts = [prefix.rstrip()]
    if additional:
        parts.append(f"\n## Additional Context\n{additional}")
    return "\n".join(parts)
