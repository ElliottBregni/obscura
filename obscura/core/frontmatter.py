"""
obscura.core.frontmatter — Parse YAML frontmatter from markdown files.

Extracts ``---`` delimited YAML metadata from the top of a markdown
file, returning both the structured metadata dict and the remaining
markdown body.

Usage::

    from obscura.core.frontmatter import parse_frontmatter

    result = parse_frontmatter(text)
    print(result.metadata)  # {"name": "dev", "tools": ["Read"]}
    print(result.body)      # "You are a developer agent..."
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


def _coerce_yaml_dict(raw: Any) -> dict[str, Any]:
    """Coerce a ``yaml.safe_load`` dict to ``dict[str, Any]``."""
    src = cast("dict[str, Any]", raw)
    return dict(src)

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\n(.*?)---[ \t]*\n(.*)\Z",
    re.DOTALL,
)


def _empty_metadata() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class FrontmatterResult:
    """Parsed frontmatter + body from a markdown file."""

    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    body: str = ""
    source_path: Path | None = None


def parse_frontmatter(
    text: str,
    *,
    source_path: Path | None = None,
) -> FrontmatterResult:
    """Parse YAML frontmatter from markdown text.

    Expects optional ``---`` delimited YAML at the very start of the text.
    Returns empty metadata if no frontmatter markers are found.

    .. note::

        This parser does not handle ``---`` inside fenced code blocks.
        If your markdown body contains ``---`` on its own line before any
        frontmatter, the parse may be incorrect.  Place frontmatter at
        the very top of the file.
    """
    if not text.strip():
        return FrontmatterResult(source_path=source_path)

    match = _FRONTMATTER_RE.match(text)
    if match is None:
        # No frontmatter — entire text is the body
        return FrontmatterResult(body=text, source_path=source_path)

    yaml_text = match.group(1)
    body = match.group(2)

    metadata: dict[str, Any] = {}
    if yaml_text.strip():
        try:
            import yaml

            parsed: Any = yaml.safe_load(yaml_text)
            if isinstance(parsed, dict):
                metadata = _coerce_yaml_dict(parsed)
            else:
                logger.warning(
                    "Frontmatter in %s is not a mapping (got %s), treating as empty",
                    source_path or "<string>",
                    type(parsed).__name__,
                )
        except Exception:
            logger.warning(
                "Malformed YAML frontmatter in %s, treating as empty",
                source_path or "<string>",
                exc_info=True,
            )

    return FrontmatterResult(
        metadata=metadata,
        body=body,
        source_path=source_path,
    )


def parse_frontmatter_file(path: Path) -> FrontmatterResult:
    """Convenience: read a file and parse its frontmatter."""
    text = path.read_text(encoding="utf-8")
    return parse_frontmatter(text, source_path=path)
