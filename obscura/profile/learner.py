"""obscura.profile.learner — Auto-detect profile facts from conversation.

Pattern-based extraction of profile-relevant statements from user messages.
Runs cheaply (no LLM call) after each turn, storing inferred facts with
lower confidence.

Usage::

    from obscura.profile.learner import ProfileLearner

    learner = ProfileLearner(profile_store)
    new_facts = learner.process_turn(user_text)
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from obscura.core.enums.storage import ProfileSource
from obscura.profile.models import ProfileCategory, ProfileFact

if TYPE_CHECKING:
    from obscura.profile.store import ProfileStore

logger = logging.getLogger(__name__)

# Regex patterns for each category. Each entry is (pattern, key_template).
# The first capture group becomes the value; key_template may use {match}.
_PATTERNS: dict[ProfileCategory, list[tuple[str, str]]] = {
    ProfileCategory.CAREER: [
        (r"I work(?:ed)? at (.+?)(?:\.|,|$)", "employer"),
        (
            r"(?:my|I'm a|I am a) (\w[\w\s]*(?:engineer|developer|scientist|designer|manager|architect|analyst))",
            "role",
        ),
        (
            r"(?:targeting|applied to|interviewing (?:at|with)) (.+?)(?:\.|,|$)",
            "target_company",
        ),
        (r"I make (?:around |about |~)?\$?([\d,]+k?)", "compensation"),
    ],
    ProfileCategory.PREFERENCE: [
        (r"I (?:always |usually )?prefer (.+?)(?:\.|,|$)", "prefers"),
        (r"I (?:really )?(?:love|like) (.+?)(?:\.|,|$)", "likes"),
        (r"I (?:really )?(?:hate|dislike|can't stand) (.+?)(?:\.|,|$)", "dislikes"),
    ],
    ProfileCategory.PERSONAL: [
        (r"I live in (.+?)(?:\.|,|$)", "location"),
        (r"I(?:'m| am) from (.+?)(?:\.|,|$)", "hometown"),
        (r"I(?:'m| am) (\d+)(?: years old)?", "age"),
    ],
    ProfileCategory.SKILL: [
        (r"I(?:'ve| have) been (?:writing|using|working with) (.+?) for", "experience"),
        (r"I know (.+?) (?:pretty |really )?well", "strong_skill"),
        (r"(?:first time|new to|learning) (.+?)(?:\.|,|$)", "learning"),
    ],
}

# Minimum text length to bother checking.
_MIN_LENGTH = 10


class ProfileLearner:
    """Cheap regex-based profile fact extractor."""

    def __init__(self, store: ProfileStore) -> None:
        self._store = store

    def process_turn(self, user_text: str) -> list[ProfileFact]:
        """Extract and store profile facts from a user message.

        Returns the list of newly stored facts (for logging/debugging).
        Only stores facts that don't already exist in the profile.
        """
        if not user_text or len(user_text) < _MIN_LENGTH:
            return []

        new_facts: list[ProfileFact] = []

        for category, patterns in _PATTERNS.items():
            for pattern, key_template in patterns:
                match = re.search(pattern, user_text, re.IGNORECASE)
                if not match:
                    continue

                value = match.group(1).strip()
                if not value or len(value) > 200:
                    continue

                key = f"{category.value}.{key_template}"

                fact = ProfileFact(
                    key=key,
                    value=value,
                    category=category,
                    confidence=0.6,
                    source=ProfileSource.INFERRED,
                )

                try:
                    self._store.set_fact(fact)
                    new_facts.append(fact)
                    logger.debug("Auto-learned profile fact: %s = %s", key, value)
                except Exception:
                    logger.debug("Failed to store inferred fact %s", key, exc_info=True)

        return new_facts


__all__ = ["ProfileLearner"]
