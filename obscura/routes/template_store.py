"""In-memory + optional SQLite persistence for APER agent templates."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory primary store (module-level singleton)
# ---------------------------------------------------------------------------

_templates: dict[str, dict[str, Any]] = {}


def get_all() -> dict[str, dict[str, Any]]:
    """Return the full in-memory template map (reference, not copy)."""
    return _templates


def get(template_id: str) -> dict[str, Any] | None:
    """Look up a template by ID."""
    return _templates.get(template_id)


def put(template_id: str, data: dict[str, Any]) -> None:
    """Insert or replace a template in memory."""
    _templates[template_id] = data


def delete(template_id: str) -> bool:
    """Remove a template. Returns ``True`` if it existed."""
    return _templates.pop(template_id, None) is not None


def clear() -> None:
    """Remove all templates (testing helper)."""
    _templates.clear()


# ---------------------------------------------------------------------------
# SQLite persistence via GlobalMemoryStore
# ---------------------------------------------------------------------------

TEMPLATE_NAMESPACE = "agent_templates"


def persist_template(template_id: str, data: dict[str, Any]) -> None:
    """Write a template to SQLite for durability across restarts."""
    try:
        from obscura.memory import GlobalMemoryStore

        store = GlobalMemoryStore.get_instance()
        store.set(template_id, data, namespace=TEMPLATE_NAMESPACE)
    except Exception:
        logger.warning(
            "Could not persist template %s to SQLite", template_id, exc_info=True
        )


def delete_persisted(template_id: str) -> None:
    """Remove a persisted template from SQLite."""
    try:
        from obscura.memory import GlobalMemoryStore

        store = GlobalMemoryStore.get_instance()
        store.delete(template_id, namespace=TEMPLATE_NAMESPACE)
    except Exception:
        logger.warning(
            "Could not delete persisted template %s", template_id, exc_info=True
        )


def load_persisted_templates() -> dict[str, dict[str, Any]]:
    """Read all persisted templates from SQLite. Called at server startup."""
    try:
        from obscura.memory import GlobalMemoryStore

        store = GlobalMemoryStore.get_instance()
        keys = store.list_keys(namespace=TEMPLATE_NAMESPACE)
        loaded: dict[str, dict[str, Any]] = {}
        for mk in keys:
            val = store.get(mk)
            if isinstance(val, dict):
                loaded[mk.key] = val
        return loaded
    except Exception:
        logger.warning(
            "Could not load persisted templates from SQLite", exc_info=True
        )
        return {}
