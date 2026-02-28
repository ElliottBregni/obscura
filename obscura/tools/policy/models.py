"""
obscura.tools.policy.models — Policy dataclasses for tool access control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _empty_frozenset() -> frozenset[str]:
    return frozenset()


@dataclass(frozen=True)
class ToolPolicy:
    """Declarative policy controlling which tools an agent may invoke.

    Evaluation order:
    1. ``full_access`` — if True, allow everything.
    2. ``deny_list`` — if the tool name matches, deny.
    3. ``allow_list`` — if non-empty, only listed tools are allowed.
    4. ``base_dir`` — if set, file-system tools are restricted to this subtree.
    """

    name: str
    allow_list: frozenset[str] = field(default_factory=_empty_frozenset)
    deny_list: frozenset[str] = field(default_factory=_empty_frozenset)
    base_dir: Path | None = None
    full_access: bool = False


    @classmethod
    def from_permission_config(
        cls,
        name: str,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        base_dir: Path | None = None,
    ) -> ToolPolicy:
        """Build a :class:`ToolPolicy` from manifest permission lists."""
        return cls(
            name=name,
            allow_list=frozenset(allow) if allow else frozenset(),
            deny_list=frozenset(deny) if deny else frozenset(),
            base_dir=base_dir,
        )


@dataclass(frozen=True)
class PolicyResult:
    """Outcome of evaluating a :class:`ToolPolicy` against a tool invocation."""

    allowed: bool
    reason: str
