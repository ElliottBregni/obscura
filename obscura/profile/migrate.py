"""obscura.profile.migrate — Migrate flat user_profile.md to vector-backed profile.

Parses the markdown sections of a user_profile.md file and creates
:class:`ProfileFact` entries in the :class:`ProfileStore`.

Usage::

    from obscura.profile.migrate import migrate_flat_profile

    count = migrate_flat_profile(Path("user_profile.md"), profile_store)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from obscura.core.enums.storage import ProfileSource
from obscura.profile.models import ProfileCategory, ProfileFact

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.profile.store import ProfileStore

logger = logging.getLogger(__name__)

# Section → category mapping.
_SECTION_MAP: dict[str, ProfileCategory] = {
    "identity": ProfileCategory.IDENTITY,
    "career": ProfileCategory.CAREER,
    "projects": ProfileCategory.SKILL,
    "working style": ProfileCategory.PREFERENCE,
    "personal": ProfileCategory.PERSONAL,
    "what obscura should always know": ProfileCategory.PREFERENCE,
    "learned": ProfileCategory.LEARNED,
}


def migrate_flat_profile(path: Path, store: ProfileStore) -> int:
    """Parse user_profile.md and store each fact in the profile store.

    Idempotent: overwrites existing keys (latest value wins).
    Returns the count of facts stored.
    """
    if not path.exists():
        logger.debug("No profile file at %s — skipping migration", path)
        return 0

    text = path.read_text(encoding="utf-8")
    sections = _parse_sections(text)
    count = 0

    for section_name, items in sections.items():
        category = _SECTION_MAP.get(section_name.lower())
        if category is None:
            continue

        for i, item in enumerate(items):
            key = _make_key(section_name, item, i)
            fact = ProfileFact(
                key=key,
                value=item,
                category=category,
                confidence=1.0,
                source=ProfileSource.USER_STATED,
            )
            store.set_fact(fact)
            count += 1

    logger.info("Migrated %d facts from %s", count, path)
    return count


def _parse_sections(text: str) -> dict[str, list[str]]:
    """Split markdown into {section_name: [bullet items]}."""
    sections: dict[str, list[str]] = {}
    current_section = ""

    for line in text.splitlines():
        # Section headers.
        header_match = re.match(r"^#{1,3}\s+(.+)", line)
        if header_match:
            current_section = header_match.group(1).strip()
            # Strip trailing formatting like " — ..."
            if " — " in current_section:
                current_section = current_section.split(" — ")[0].strip()
            sections.setdefault(current_section, [])
            continue

        # Bullet items.
        bullet_match = re.match(r"^[-*]\s+(.+)", line)
        if bullet_match and current_section:
            item = bullet_match.group(1).strip()
            # Strip markdown bold markers for cleaner storage.
            item = re.sub(r"\*\*(.+?)\*\*", r"\1", item)
            if item and not item.startswith("<!--"):
                sections[current_section].append(item)

    return sections


def _make_key(section: str, item: str, index: int) -> str:
    """Generate a stable key from section name and item content."""
    # Use the label before the colon if present, else truncated content.
    section_slug = re.sub(r"[^\w]", "_", section.lower()).strip("_")

    if ":" in item:
        label = item.split(":", maxsplit=1)[0].strip()
        label_slug = re.sub(r"[^\w]", "_", label.lower()).strip("_")
        return f"{section_slug}.{label_slug}"

    # Fall back to section + index.
    return f"{section_slug}.item_{index}"


__all__ = ["migrate_flat_profile"]
