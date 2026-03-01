"""Markdown skill document loading from .obscura directories."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obscura.core.frontmatter import parse_frontmatter
from obscura.core.paths import resolve_obscura_skills_dir


def _empty_metadata() -> dict[str, Any]:
    return {}


def _empty_tools() -> tuple[str, ...]:
    return ()


@dataclass(frozen=True)
class MarkdownSkillDocument:
    """A markdown skill document loaded from disk.

    If the file has YAML frontmatter, ``metadata`` holds the parsed dict,
    ``body`` holds the markdown after frontmatter, and ``description``,
    ``user_invocable``, ``allowed_tools`` are populated from the metadata.

    ``content`` always holds the full raw text (for backward compat).
    """

    name: str
    path: Path
    content: str
    # Frontmatter-derived fields
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    description: str = ""
    user_invocable: bool = True
    allowed_tools: tuple[str, ...] = field(default_factory=_empty_tools)
    body: str = ""


def load_markdown_skill_documents(
    skills_root: str | Path | None = None,
) -> list[MarkdownSkillDocument]:
    """Load markdown skills from ``.obscura/skills`` recursively.

    Parses YAML frontmatter if present, populating metadata fields.
    """
    root = (
        resolve_obscura_skills_dir()
        if skills_root is None
        else Path(skills_root).expanduser().resolve()
    )
    if not root.is_dir():
        return []

    documents: list[MarkdownSkillDocument] = []
    for skill_path in sorted(root.rglob("*.md")):
        if not skill_path.is_file():
            continue
        content = skill_path.read_text(encoding="utf-8").strip()
        if not content:
            continue

        rel_path = skill_path.relative_to(root)
        skill_name = rel_path.with_suffix("").as_posix()

        result = parse_frontmatter(content, source_path=skill_path)
        meta = result.metadata

        # Extract frontmatter fields
        raw_tools: Any = meta.get("allowed-tools", meta.get("allowed_tools"))
        allowed: tuple[str, ...] = ()
        if isinstance(raw_tools, list):
            allowed = tuple(str(t) for t in raw_tools)

        documents.append(
            MarkdownSkillDocument(
                name=str(meta.get("name", skill_name)),
                path=skill_path,
                content=content,
                metadata=meta,
                description=str(meta.get("description", "")),
                user_invocable=bool(meta.get("user-invocable", meta.get("user_invocable", True))),
                allowed_tools=allowed,
                body=result.body.strip(),
            )
        )
    return documents


def load_markdown_skill_texts(skills_root: str | Path | None = None) -> list[str]:
    """Convenience accessor for raw markdown skill text blocks."""
    return [doc.content for doc in load_markdown_skill_documents(skills_root)]
