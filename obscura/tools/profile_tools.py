"""obscura.tools.profile_tools — User profile management tools.

Provides six tools for viewing and managing the user profile:
  - profile_get:      Read the full profile or a compact summary
  - profile_update:   Append a new fact/preference/episode
  - profile_recall:   Semantically search profile facts
  - profile_sync:     Migrate flat profile to vector store
  - profile_set:      Set a structured profile fact (vector-backed, with decay)
  - profile_forget:   Remove a profile fact from the vector store

The profile lives in two layers:
  1. ``~/.obscura/user_profile.md`` — human-readable markdown (legacy)
  2. Vector store with per-category decay via :class:`ProfileStore`

The vector-backed layer adds proper decay:
  - identity   → immune (name, email never decay)
  - career     → 90-day half-life
  - skill      → 120-day half-life
  - preference → 180-day half-life
  - personal   → 60-day half-life
  - learned    → 30-day half-life
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from obscura.core.tools import tool

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec


def _profile() -> Any:
    from obscura.kairos.user_profile import UserProfile

    return UserProfile()


def _profile_store() -> Any:
    """Get the vector-backed ProfileStore (lazy, may fail if no user context)."""
    try:
        from obscura.auth.context import current_user
        from obscura.profile.store import ProfileStore

        user = current_user()
        return ProfileStore.for_user(user)
    except Exception:
        return None


def _profile_builder() -> Any:
    from obscura.profile.builder import ProfileBuilder

    return ProfileBuilder()


def _json_ok(**data: object) -> str:
    payload: dict[str, object] = {"ok": True}
    payload.update(data)
    return json.dumps(payload)


def _json_error(error: str, **extra: object) -> str:
    payload: dict[str, object] = {"ok": False, "error": error}
    payload.update(extra)
    return json.dumps(payload)


@tool(
    "profile_get",
    "Read the user profile. Returns compact summary (vector-backed if available, "
    "falls back to markdown file).",
    {
        "type": "object",
        "properties": {
            "compact": {
                "type": "boolean",
                "description": "If true (default), return a compact summary. "
                               "If false, return the full profile text.",
            },
            "include_scores": {
                "type": "boolean",
                "description": "Show decay scores for each fact (vector store only).",
            },
        },
    },
)
def profile_get(compact: bool = True, include_scores: bool = False) -> str:
    # Try vector-backed profile first.
    store = _profile_store()
    if store is not None:
        if include_scores:
            all_facts = store.get_all_facts()
            fact_data = [
                {
                    "key": f.key,
                    "value": f.value,
                    "category": f.category.value,
                    "score": round(score, 3),
                    "source": f.source,
                }
                for f, score in all_facts
            ]
            return _json_ok(facts=fact_data, count=len(fact_data), source="vector")

        if compact:
            builder = _profile_builder()
            summary = builder.build_summary(store)
            if summary:
                return _json_ok(summary=summary, source="vector")

    # Fall back to markdown file.
    p = _profile()
    if not p.exists():
        return _json_error("profile_not_found", hint="Use profile_update to start building it.")
    if compact:
        return _json_ok(summary=p.active_summary(), source="markdown")
    return _json_ok(profile=p.read(), source="markdown")


@tool(
    "profile_update",
    "Append a new fact, preference, or episode to the user profile. "
    "Writes to both the markdown file and vector store.",
    {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The fact or observation to record. Be specific and concise.",
            },
            "memory_type": {
                "type": "string",
                "enum": ["fact", "preference", "episode"],
                "description": (
                    "Decay category. "
                    "fact=90-day half-life (career info, skills, background). "
                    "preference=permanent (working style, tool preferences, hates/loves). "
                    "episode=7-day half-life (recent events, current projects, short-lived context)."
                ),
            },
        },
        "required": ["fact"],
    },
)
def profile_update(fact: str, memory_type: str = "fact") -> str:
    valid_types = {"fact", "preference", "episode"}
    if memory_type not in valid_types:
        memory_type = "fact"

    # Write to markdown file.
    p = _profile()
    appended = p.append_fact(fact, memory_type=memory_type)

    # Also store as a structured fact in the vector-backed profile.
    store = _profile_store()
    if store is not None:
        try:
            from obscura.profile.models import ProfileCategory, ProfileFact

            # Map legacy memory_type to new category.
            _type_to_category = {
                "fact": ProfileCategory.CAREER,
                "preference": ProfileCategory.PREFERENCE,
                "episode": ProfileCategory.LEARNED,
            }
            category = _type_to_category.get(memory_type, ProfileCategory.LEARNED)

            import re
            import time

            key = f"{category.value}.{re.sub(r'[^\\w]', '_', fact[:40].lower()).strip('_')}_{int(time.time()) % 10000}"
            structured_fact = ProfileFact(
                key=key,
                value=fact,
                category=category,
                confidence=1.0,
                source="user_stated",
            )
            store.set_fact(structured_fact)
        except Exception:
            pass  # vector store is best-effort

    if appended:
        return _json_ok(appended=True, fact=fact, memory_type=memory_type)
    return _json_ok(appended=False, reason="duplicate", fact=fact)


@tool(
    "profile_recall",
    "Semantically search the user profile for facts relevant to a query.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query to search profile facts.",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results (default 5).",
            },
        },
        "required": ["query"],
    },
)
def profile_recall(query: str, top_k: int = 5) -> str:
    p = _profile()
    results = p.semantic_recall(query, top_k=top_k)
    return _json_ok(results=results, count=len(results))


@tool(
    "profile_sync",
    "Migrate the flat user_profile.md to the vector store with per-category decay. "
    "Idempotent — safe to run multiple times.",
    {
        "type": "object",
        "properties": {},
    },
)
def profile_sync() -> str:
    store = _profile_store()
    if store is None:
        # Fall back to legacy sync.
        p = _profile()
        synced = p.sync_to_vector_store()
        return _json_ok(synced=synced, method="legacy")

    # Use new migration path.
    try:
        from pathlib import Path

        from obscura.profile.migrate import migrate_flat_profile

        # Try project-level first, then home.
        profile_path = Path("user_profile.md")
        if not profile_path.exists():
            profile_path = Path.home() / ".obscura" / "user_profile.md"

        count = migrate_flat_profile(profile_path, store)
        return _json_ok(synced=count, method="vector_migration")
    except Exception as e:
        return _json_error(f"Migration failed: {e}")


@tool(
    "profile_set",
    "Set a structured profile fact with per-category decay. "
    "Identity facts never decay; learned facts decay in 30 days.",
    {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Fact key (e.g. 'career.target_company', 'personal.location').",
            },
            "value": {
                "type": "string",
                "description": "The fact value.",
            },
            "category": {
                "type": "string",
                "enum": ["identity", "career", "skill", "preference", "personal", "learned"],
                "description": "Fact category (determines decay rate).",
            },
        },
        "required": ["key", "value", "category"],
    },
)
def profile_set(key: str, value: str, category: str) -> str:
    store = _profile_store()
    if store is None:
        return _json_error("Vector-backed profile not available (no user context)")

    from obscura.profile.models import ProfileCategory, ProfileFact

    try:
        cat = ProfileCategory(category)
    except ValueError:
        return _json_error(f"Unknown category: {category}")

    fact = ProfileFact(
        key=key,
        value=value,
        category=cat,
        confidence=1.0,
        source="user_stated",
    )
    store.set_fact(fact)
    return _json_ok(key=key, value=value, category=category)


@tool(
    "profile_forget",
    "Remove a profile fact from the vector store by key.",
    {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The fact key to remove.",
            },
        },
        "required": ["key"],
    },
)
def profile_forget(key: str) -> str:
    store = _profile_store()
    if store is None:
        return _json_error("Vector-backed profile not available (no user context)")

    deleted = store.forget(key)
    if deleted:
        return _json_ok(key=key, deleted=True)
    return _json_error(f"Fact not found: {key}")


def get_profile_tool_specs() -> list[ToolSpec]:
    """Return profile management tool specs for registration."""
    return [
        cast("ToolSpec", profile_get.spec),
        cast("ToolSpec", profile_update.spec),
        cast("ToolSpec", profile_recall.spec),
        cast("ToolSpec", profile_sync.spec),
        cast("ToolSpec", profile_set.spec),
        cast("ToolSpec", profile_forget.spec),
    ]
