"""obscura.profile.models — Data models for the vector-backed user profile.

Defines profile fact categories with per-category decay profiles, and the
frozen ProfileFact dataclass that serves as the unit of storage.

Usage::

    from obscura.profile.models import ProfileFact, ProfileCategory

    fact = ProfileFact(
        key="career.target_company",
        value="Datadog",
        category=ProfileCategory.CAREER,
        confidence=1.0,
        source="user_stated",
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from obscura.vector_memory.decay import DecayProfile


class ProfileCategory(StrEnum):
    """Profile fact categories with distinct decay behaviors."""

    IDENTITY = "identity"  # name, email, location — immune to decay
    CAREER = "career"  # role, company, targets — 90-day half-life
    SKILL = "skill"  # languages, frameworks — 120-day half-life
    PREFERENCE = "preference"  # working style, tools — 180-day half-life
    PERSONAL = "personal"  # hobbies, restaurants, travel — 60-day half-life
    LEARNED = "learned"  # ephemeral observations — 30-day half-life


#: Maps each category to its decay profile.  These are registered with the
#: vector memory store's decay config so that ``compute_decay`` handles them
#: transparently.
PROFILE_DECAY: dict[ProfileCategory, DecayProfile] = {
    ProfileCategory.IDENTITY: DecayProfile(immune=True),
    ProfileCategory.CAREER: DecayProfile(half_life_days=90.0, min_score_floor=0.05),
    ProfileCategory.SKILL: DecayProfile(half_life_days=120.0, min_score_floor=0.05),
    ProfileCategory.PREFERENCE: DecayProfile(
        half_life_days=180.0, min_score_floor=0.01
    ),
    ProfileCategory.PERSONAL: DecayProfile(half_life_days=60.0, min_score_floor=0.02),
    ProfileCategory.LEARNED: DecayProfile(half_life_days=30.0, min_score_floor=0.01),
}

#: Prefix for memory_type strings stored in vector memory.
PROFILE_TYPE_PREFIX = "profile_"


def memory_type_for_category(category: ProfileCategory) -> str:
    """Return the ``memory_type`` string used in vector memory for *category*."""
    return f"{PROFILE_TYPE_PREFIX}{category.value}"


@dataclass(frozen=True)
class ProfileFact:
    """A single user-profile fact stored in vector memory.

    Facts are the atomic unit of the profile system.  Each fact belongs to a
    :class:`ProfileCategory` that determines how quickly it decays.  Facts
    can be *user_stated* (explicit), *inferred* (from conversation patterns),
    or *observed* (from agent-side signals like tool usage).
    """

    key: str
    value: str
    category: ProfileCategory
    confidence: float = 1.0
    source: Literal["user_stated", "inferred", "observed"] = "user_stated"
    learned_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    supersedes: str | None = None

    @property
    def memory_type(self) -> str:
        return memory_type_for_category(self.category)

    @property
    def search_text(self) -> str:
        """Text stored as the vector memory entry for embedding."""
        return f"{self.category.value}: {self.key} = {self.value}"

    def to_metadata(self) -> dict[str, str | float]:
        """Serialize to metadata dict for vector memory storage."""
        meta: dict[str, str | float] = {
            "key": self.key,
            "category": self.category.value,
            "confidence": self.confidence,
            "source": self.source,
            "learned_at": self.learned_at,
        }
        if self.supersedes:
            meta["supersedes"] = self.supersedes
        return meta


def register_profile_decay_profiles(profiles: dict[str, DecayProfile]) -> None:
    """Merge profile decay profiles into an existing profiles dict.

    Call this during store initialization so that ``compute_decay`` recognizes
    the ``profile_*`` memory types.
    """
    for category, decay in PROFILE_DECAY.items():
        profiles[memory_type_for_category(category)] = decay
