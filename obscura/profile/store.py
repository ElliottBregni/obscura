"""obscura.profile.store — Vector-backed profile store with per-category decay.

Stores :class:`ProfileFact` entries in the vector memory system.  Each fact
is a vector entry in the ``profile:<user_id>`` namespace, and its
``memory_type`` is derived from the fact's :class:`ProfileCategory` so that
the existing decay math applies automatically.

Usage::

    from obscura.profile.store import ProfileStore

    store = ProfileStore.for_user(user)
    await store.set_fact(fact)
    active = await store.get_active_profile()
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from obscura.memory import MemoryKey
from obscura.profile.models import (
    ProfileCategory,
    ProfileFact,
    register_profile_decay_profiles,
)
from obscura.vector_memory.decay import compute_decay
from obscura.vector_memory.vector_memory import VectorMemoryStore as VMS

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser
    from obscura.vector_memory.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

_NAMESPACE_PREFIX = "profile"


class ProfileStore:
    """Vector-backed user profile with per-category decay.

    Wraps a :class:`VectorMemoryStore` and exposes profile-specific CRUD.
    Facts are stored with ``memory_type=profile_<category>`` so the existing
    decay infrastructure handles freshness transparently.
    """

    _instances: dict[str, ProfileStore] = {}
    _lock = threading.Lock()

    def __init__(
        self,
        vector_store: VectorMemoryStore,
        user_id: str,
    ) -> None:
        self._store = vector_store
        self._user_id = user_id
        self._namespace = f"{_NAMESPACE_PREFIX}:{user_id}"

        # Register profile decay profiles into the store's config.
        register_profile_decay_profiles(self._store.decay_config.profiles)

    @classmethod
    def for_user(
        cls,
        user: AuthenticatedUser,
        vector_store: VectorMemoryStore | None = None,
    ) -> ProfileStore:
        """Return (or create) a singleton ProfileStore for *user*."""
        with cls._lock:
            if user.user_id in cls._instances:
                return cls._instances[user.user_id]

            if vector_store is None:
                vector_store = VMS.for_user(user)

            instance = cls(vector_store, user.user_id)
            cls._instances[user.user_id] = instance
            return instance

    # -- Write ----------------------------------------------------------------

    def set_fact(self, fact: ProfileFact) -> None:
        """Store a profile fact in vector memory.

        If *fact.supersedes* names another key, the old entry is deleted first.
        """
        if fact.supersedes:
            self.forget(fact.supersedes)

        self._store.set(
            key=f"profile:{fact.key}",
            text=fact.search_text,
            metadata=fact.to_metadata(),
            namespace=self._namespace,
            memory_type=fact.memory_type,
        )
        logger.debug("Profile fact stored: %s = %s", fact.key, fact.value)

    def forget(self, key: str) -> bool:
        """Delete a profile fact by key."""
        full_key = MemoryKey(namespace=self._namespace, key=f"profile:{key}")
        try:
            self._store.backend.delete_vector(full_key)
            logger.debug("Profile fact deleted: %s", key)
            return True
        except Exception:
            logger.debug("Could not delete profile fact %s", key, exc_info=True)
            return False

    def refresh(self, key: str) -> None:
        """Touch a fact to reset its accessed_at and boost it above decay."""
        full_key = MemoryKey(namespace=self._namespace, key=f"profile:{key}")
        try:
            self._store.backend.touch_vector(full_key)
        except Exception:
            logger.debug("Could not refresh profile fact %s", key, exc_info=True)

    # -- Read -----------------------------------------------------------------

    def get_all_facts(self) -> list[tuple[ProfileFact, float]]:
        """Return all profile facts with their current decay scores.

        Sorted descending by score (freshest / most relevant first).
        """
        try:
            entries = self._store.backend.list_keys(namespace=self._namespace)
        except Exception:
            logger.debug("Could not list profile facts", exc_info=True)
            return []

        results: list[tuple[ProfileFact, float]] = []
        now = datetime.now(UTC)

        for entry_key in entries:
            try:
                entry = self._store.backend.get_vector(entry_key)
                if entry is None:
                    continue

                meta = entry.metadata or {}
                category_str = meta.get("category", "learned")
                try:
                    category = ProfileCategory(category_str)
                except ValueError:
                    logger.debug("suppressed exception in get_all_facts", exc_info=True)
                    category = ProfileCategory.LEARNED

                fact = ProfileFact(
                    key=str(meta.get("key", entry_key)),
                    value=entry.text.split(" = ", 1)[-1]
                    if " = " in entry.text
                    else entry.text,
                    category=category,
                    confidence=float(meta.get("confidence", 1.0)),
                    source=meta.get("source", "user_stated"),  # type: ignore[arg-type]
                    learned_at=str(meta.get("learned_at", "")),
                    supersedes=meta.get("supersedes"),  # type: ignore[arg-type]
                )

                score = compute_decay(
                    fact.memory_type,
                    entry.created_at if hasattr(entry, "created_at") else now,
                    getattr(entry, "accessed_at", None),
                    self._store.decay_config,
                    now=now,
                )
                results.append((fact, score))
            except Exception:
                logger.debug(
                    "Skipping malformed profile entry %s", entry_key, exc_info=True
                )

        results.sort(key=lambda pair: pair[1], reverse=True)
        return results

    def get_active_profile(self, min_score: float = 0.1) -> list[ProfileFact]:
        """Return only facts above the decay threshold, sorted by score."""
        return [fact for fact, score in self.get_all_facts() if score >= min_score]

    def get_facts_by_category(
        self,
        category: ProfileCategory,
        min_score: float = 0.0,
    ) -> list[ProfileFact]:
        """Return facts filtered by category."""
        return [
            fact
            for fact, score in self.get_all_facts()
            if fact.category == category and score >= min_score
        ]


__all__ = ["ProfileStore"]
