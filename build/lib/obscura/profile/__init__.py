"""obscura.profile — Vector-backed user profile with per-category decay.

Provides a persistent user profile system where facts decay at different
rates depending on their category (identity is immune, learned facts decay
in 30 days).  All storage is delegated to the vector memory system.

Public API::

    from obscura.profile import (
        ProfileBuilder,
        ProfileCategory,
        ProfileFact,
        ProfileLearner,
        ProfileStore,
        migrate_flat_profile,
    )
"""

from __future__ import annotations

from obscura.profile.builder import ProfileBuilder
from obscura.profile.learner import ProfileLearner
from obscura.profile.migrate import migrate_flat_profile
from obscura.profile.models import ProfileCategory, ProfileFact
from obscura.profile.store import ProfileStore

__all__ = [
    "ProfileBuilder",
    "ProfileCategory",
    "ProfileFact",
    "ProfileLearner",
    "ProfileStore",
    "migrate_flat_profile",
]
