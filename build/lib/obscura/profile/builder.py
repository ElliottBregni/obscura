"""obscura.profile.builder — Assemble a compact profile summary for system prompt injection.

Reads active profile facts from a :class:`ProfileStore`, groups them by
category, and renders a concise text block suitable for inclusion in every
agent's system prompt (~300-400 tokens).

Usage::

    from obscura.profile.builder import ProfileBuilder

    builder = ProfileBuilder()
    summary = builder.build_summary(profile_store, max_tokens=400)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.profile.models import ProfileCategory, ProfileFact

if TYPE_CHECKING:
    from obscura.profile.store import ProfileStore

# Rough chars-per-token estimate for compact text.
_CHARS_PER_TOKEN = 4

# Category display order and labels.
_CATEGORY_ORDER: list[tuple[ProfileCategory, str]] = [
    (ProfileCategory.IDENTITY, "Identity"),
    (ProfileCategory.CAREER, "Career"),
    (ProfileCategory.SKILL, "Skills"),
    (ProfileCategory.PREFERENCE, "Working Style"),
    (ProfileCategory.PERSONAL, "Personal"),
    (ProfileCategory.LEARNED, "Recent"),
]


class ProfileBuilder:
    """Builds a compact user profile summary from active facts."""

    def build_summary(
        self,
        store: ProfileStore,
        *,
        max_tokens: int = 400,
        min_score: float = 0.1,
    ) -> str:
        """Render a profile summary from active facts.

        Groups facts by category in display order, identity first.
        Truncates at *max_tokens* (estimated via char count).

        Returns empty string if no active facts.
        """
        facts = store.get_active_profile(min_score=min_score)
        if not facts:
            return ""

        # Group by category.
        by_category: dict[ProfileCategory, list[ProfileFact]] = {}
        for fact in facts:
            by_category.setdefault(fact.category, []).append(fact)

        max_chars = max_tokens * _CHARS_PER_TOKEN
        lines: list[str] = []
        char_count = 0

        for category, label in _CATEGORY_ORDER:
            cat_facts = by_category.get(category, [])
            if not cat_facts:
                continue

            header = f"**{label}**:"
            # Compact rendering: key=value pairs joined inline.
            if category == ProfileCategory.IDENTITY:
                # Identity gets special one-line treatment.
                parts = [f.value for f in cat_facts[:5]]
                line = f"{header} {' | '.join(parts)}"
            else:
                parts = [f"{f.key}: {f.value}" for f in cat_facts[:6]]
                line = f"{header} {', '.join(parts)}"

            if char_count + len(line) > max_chars:
                # Truncate remaining categories.
                break
            lines.append(line)
            char_count += len(line) + 1  # +1 for newline

        return "\n".join(lines)


__all__ = ["ProfileBuilder"]
