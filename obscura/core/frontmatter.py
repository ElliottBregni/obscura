"""
obscura.core.frontmatter — Parse frontmatter from markdown files.

Supports TOML frontmatter (``+++`` delimited, preferred) and YAML
frontmatter (``---`` delimited, deprecated).

Usage::

    from obscura.core.frontmatter import parse_frontmatter

    result = parse_frontmatter(text)
    print(result.metadata)  # {"name": "dev", "tools": ["Read"]}
    print(result.body)      # "You are a developer agent..."
"""

from __future__ import annotations

import logging
import re
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


def _coerce_yaml_dict(raw: Any) -> dict[str, Any]:
    """Coerce a ``yaml.safe_load`` dict to ``dict[str, Any]``."""
    src = cast("dict[str, Any]", raw)
    return dict(src)


_TOML_FRONTMATTER_RE = re.compile(
    r"\A\+\+\+[ \t]*\n(.*?)\+\+\+[ \t]*\n(.*)\Z",
    re.DOTALL,
)

_YAML_FRONTMATTER_RE = re.compile(
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
    """Parse frontmatter from markdown text.

    Tries TOML frontmatter (``+++`` delimiters) first, then falls back to
    YAML frontmatter (``---`` delimiters, deprecated).
    Returns empty metadata if no frontmatter markers are found.
    """
    if not text.strip():
        return FrontmatterResult(source_path=source_path)

    # Try TOML frontmatter first (preferred)
    match = _TOML_FRONTMATTER_RE.match(text)
    if match is not None:
        toml_text = match.group(1)
        body = match.group(2)
        metadata: dict[str, Any] = {}
        if toml_text.strip():
            try:
                metadata = tomllib.loads(toml_text)
            except Exception:
                logger.warning(
                    "Malformed TOML frontmatter in %s, treating as empty",
                    source_path or "<string>",
                    exc_info=True,
                )
        return FrontmatterResult(
            metadata=metadata,
            body=body,
            source_path=source_path,
        )

    # Fall back to YAML frontmatter (deprecated)
    match = _YAML_FRONTMATTER_RE.match(text)
    if match is None:
        # No frontmatter — entire text is the body
        return FrontmatterResult(body=text, source_path=source_path)

    warnings.warn(
        f"YAML frontmatter (---) is deprecated; use TOML frontmatter (+++) instead. "
        f"Source: {source_path or '<string>'}",
        DeprecationWarning,
        stacklevel=2,
    )

    yaml_text = match.group(1)
    body = match.group(2)

    metadata = {}
    if yaml_text.strip():
        try:
            import yaml  # type: ignore[import-untyped]

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
