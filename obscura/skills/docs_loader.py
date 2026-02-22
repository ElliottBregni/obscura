"""Markdown skill document loading from .obscura directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from obscura.core.paths import resolve_obscura_skills_dir


@dataclass(frozen=True)
class MarkdownSkillDocument:
    """A markdown skill document loaded from disk."""

    name: str
    path: Path
    content: str


def load_markdown_skill_documents(
    skills_root: str | Path | None = None,
) -> list[MarkdownSkillDocument]:
    """Load markdown skills from ``.obscura/skills`` recursively."""
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
        documents.append(
            MarkdownSkillDocument(
                name=skill_name,
                path=skill_path,
                content=content,
            )
        )
    return documents


def load_markdown_skill_texts(skills_root: str | Path | None = None) -> list[str]:
    """Convenience accessor for raw markdown skill text blocks."""
    return [doc.content for doc in load_markdown_skill_documents(skills_root)]
