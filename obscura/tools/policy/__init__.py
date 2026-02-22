"""
obscura.tools.policy — First-class tool access control.

Policy objects replace env-var-only tool restrictions with structured,
composable rules that can be evaluated at runtime.
"""

from __future__ import annotations

from obscura.tools.policy.engine import evaluate_policy
from obscura.tools.policy.models import PolicyResult, ToolPolicy

__all__ = [
    "ToolPolicy",
    "PolicyResult",
    "evaluate_policy",
]
