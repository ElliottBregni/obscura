"""obscura.kairos.user_profile — Persistent user profile for KAIROS.

The user profile is the primary source of truth about who is running Obscura.
It lives at ``~/.obscura/user_profile.md`` and is structured as a markdown
file with a ``## Learned`` section that agents append new facts to.

Design:
  - Markdown file is the human-readable source of truth (easy to edit)
  - Vector store holds individual facts with per-type decay:
      preference  → immune (never decays)
      fact        → 90-day half-life
      episode     → 7-day half-life
  - KAIROS injects a compact summary into every system prompt
  - Dream Phase 0 scans sessions for new user facts and appends them
  - Any agent can call ``profile_update()`` / ``profile_get()``

Usage::

    profile = UserProfile()
    summary = profile.active_summary()   # injected into system prompt
    profile.append_fact("Prefers dark mode")
    profile.sync_to_vector_store()       # push pending items to Qdrant/SQLite
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

_PROFILE_PATH = Path.home() / ".obscura" / "user_profile.md"
_VECTOR_NAMESPACE = "user_profile"

# How many lines of profile to show in system prompt.
_SUMMARY_MAX_LINES = 20


class UserProfile:
    """Read/write interface over ``~/.obscura/user_profile.md``.

    The profile is stored in two forms:
    1. Markdown file — human-editable, full history
    2. Vector store — per-fact with proper decay for semantic retrieval

    Parameters
    ----------
    profile_path:
        Override the default profile path (useful for testing).

    """

    def __init__(self, profile_path: Path | None = None) -> None:
        self._path = profile_path or _PROFILE_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def exists(self) -> bool:
        """True if the profile file exists."""
        return self._path.exists()

    def read(self) -> str:
        """Return the full profile markdown text."""
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")

    def active_summary(self, max_lines: int = _SUMMARY_MAX_LINES) -> str:
        """Return a compact summary for system prompt injection.

        Pulls the first ``max_lines`` lines of the profile, skipping
        empty lines and HTML comments.  Falls back to empty string if
        the profile doesn't exist.
        """
        text = self.read()
        if not text:
            return ""

        output_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            # Skip HTML comments and blank lines for the compact view.
            if not stripped or stripped.startswith("<!--"):
                continue
            output_lines.append(line)
            if len(output_lines) >= max_lines:
                remaining = sum(
                    1
                    for ln in text.splitlines()[len(output_lines) :]
                    if ln.strip() and not ln.strip().startswith("<!--")
                )
                if remaining > 0:
                    output_lines.append(f"_...and {remaining} more lines_")
                break

        return "\n".join(output_lines)

    def get_learned_facts(self) -> list[str]:
        """Return the list of timestamped facts from the ``## Learned`` section."""
        text = self.read()
        if not text:
            return []

        in_learned = False
        facts: list[str] = []
        for line in text.splitlines():
            if line.strip().startswith("## Learned"):
                in_learned = True
                continue
            if in_learned:
                # Stop at next section header.
                if line.startswith("## ") or line.startswith("# "):
                    break
                stripped = line.strip()
                if stripped.startswith("- ") and len(stripped) > 2:
                    facts.append(stripped[2:])
        return facts

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append_fact(self, fact: str, *, memory_type: str = "fact") -> bool:
        """Append a new timestamped fact to the ``## Learned`` section.

        Parameters
        ----------
        fact:
            The fact text to append (one line).
        memory_type:
            Hint for vector store decay: ``"fact"``, ``"preference"``,
            ``"episode"``.

        Returns True if the fact was appended (False if duplicate).
        """
        fact = fact.strip()
        if not fact:
            return False

        # Deduplicate: don't append if an identical fact already exists.
        existing = self.get_learned_facts()
        existing_bare = [re.sub(r"^\d{4}-\d{2}-\d{2}: ?", "", f) for f in existing]
        if fact in existing_bare or fact in existing:
            return False

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entry = f"- {today}: {fact}"

        text = self.read()
        if not text:
            text = self._bootstrap_template()

        if "## Learned" in text:
            lines = text.splitlines()
            lines.append(entry)
            text = "\n".join(lines) + "\n"
        else:
            text = (
                text.rstrip("\n")
                + f"\n\n## Learned\n<!-- Agents append facts here -->\n\n{entry}\n"
            )

        self._path.write_text(text, encoding="utf-8")
        logger.info("UserProfile: appended fact: %s", fact[:80])

        # Also sync this single fact to the vector store.
        self._sync_fact_to_vector(fact, memory_type=memory_type)
        return True

    def sync_to_vector_store(self) -> int:
        """Sync all profile sections to the vector store with proper decay types.

        Returns the number of entries synced.
        """
        text = self.read()
        if not text:
            return 0

        synced = 0
        try:
            from obscura.auth.context import current_user  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            from obscura.vector_memory.vector_memory import VectorMemoryStore

            store = VectorMemoryStore(user=cast(Any, current_user()))

            sections = self._parse_sections(text)
            for section_name, section_text in sections.items():
                memory_type = _section_to_memory_type(section_name)
                items = _extract_bullet_items(section_text)
                for item in items:
                    if len(item) < 10:
                        continue
                    try:
                        key = f"profile:{_slugify(section_name)}:{_slugify(item[:40])}"
                        store.set(
                            key,
                            item,
                            namespace=_VECTOR_NAMESPACE,
                            memory_type=memory_type,
                        )
                        synced += 1
                    except Exception:
                        logger.debug(
                            "UserProfile: failed to sync item: %s",
                            item[:60],
                            exc_info=True,
                        )

        except ImportError:
            logger.debug("VectorMemoryStore unavailable, skipping vector sync")
        except Exception:
            logger.debug("UserProfile: vector sync failed", exc_info=True)

        return synced

    def semantic_recall(self, query: str, top_k: int = 5) -> list[str]:
        """Semantically search the profile vector store."""
        try:
            from obscura.auth.context import current_user  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            from obscura.vector_memory.vector_memory import VectorMemoryStore

            store = VectorMemoryStore(user=cast(Any, current_user()))
            results = store.search_reranked(
                query=query,
                namespace=_VECTOR_NAMESPACE,
                top_k=top_k,
            )
            return [r.text for r in results]
        except Exception:
            logger.debug("UserProfile: semantic recall failed", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sync_fact_to_vector(self, fact: str, *, memory_type: str = "fact") -> None:
        """Sync a single fact string to the vector store."""
        try:
            from obscura.auth.context import current_user  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            from obscura.vector_memory.vector_memory import VectorMemoryStore

            store = VectorMemoryStore(user=cast(Any, current_user()))
            key = f"profile:learned:{_slugify(fact[:40])}:{int(time.time()) % 100000}"
            store.set(key, fact, namespace=_VECTOR_NAMESPACE, memory_type=memory_type)
        except Exception:
            logger.debug("UserProfile: single-fact vector sync failed", exc_info=True)

    def _parse_sections(self, text: str) -> dict[str, str]:
        """Split profile markdown into {section_name: section_body} dict."""
        sections: dict[str, str] = {}
        current_section = "General"
        current_lines: list[str] = []

        for line in text.splitlines():
            if line.startswith("## "):
                if current_lines:
                    sections[current_section] = "\n".join(current_lines)
                current_section = line[3:].strip()
                current_lines = []
            elif not line.startswith("# "):
                current_lines.append(line)

        if current_lines:
            sections[current_section] = "\n".join(current_lines)

        return sections

    def _bootstrap_template(self) -> str:
        """Return a minimal profile template when no file exists."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        return (
            f"# User Profile\n"
            f"<!-- AUTO-LOADED into every Obscura session via memory channel -->\n"
            f"<!-- Agents: append new learnings to ## Learned at the bottom -->\n"
            f"<!-- Last updated: {today} -->\n\n"
            f"---\n\n"
            f"## Learned\n"
            f"<!-- Agents append timestamped facts here as they learn them -->\n\n"
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _section_to_memory_type(section_name: str) -> str:
    """Map a profile section name to a vector memory decay type."""
    name_lower = section_name.lower()
    if "prefer" in name_lower or "style" in name_lower or "should always" in name_lower:
        return "preference"
    if "learned" in name_lower or "personal" in name_lower:
        return "episode"
    return "fact"


def _extract_bullet_items(text: str) -> list[str]:
    """Extract bullet point items from markdown text."""
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            items.append(stripped[2:].strip())
        elif stripped.startswith("**") and ":" in stripped:
            items.append(stripped)
    return [i for i in items if i]


def _slugify(text: str) -> str:
    """Convert text to a safe key slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")[:50] or "item"
