"""
obscura.core.templates — Reusable job/task templates.

Templates are markdown files stored in ``~/.obscura/templates/``
with TOML frontmatter defining the template metadata.

Usage::

    from obscura.core.templates import list_templates, load_template, run_template

    templates = list_templates()
    tmpl = load_template("code-review")
    prompt = tmpl.render({"file": "src/main.py"})
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from obscura.core.frontmatter import parse_frontmatter_file

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path.home() / ".obscura" / "templates"


@dataclass(frozen=True)
class Template:
    """A reusable task template."""

    name: str
    description: str = ""
    prompt: str = ""
    variables: tuple[str, ...] = ()  # placeholder names like {{file}}, {{query}}
    allowed_tools: tuple[str, ...] = ()  # optional tool restriction
    source_path: str = ""

    def render(self, variables: dict[str, str] | None = None) -> str:
        """Render the template prompt with variable substitution."""
        result = self.prompt
        for key, value in (variables or {}).items():
            result = result.replace("{{" + key + "}}", value)
        return result


def load_template(name: str) -> Template | None:
    """Load a template by name from the templates directory."""
    path = _TEMPLATES_DIR / f"{name}.md"
    if not path.is_file():
        # Try without extension.
        candidates = list(_TEMPLATES_DIR.glob(f"{name}*"))
        if candidates:
            path = candidates[0]
        else:
            return None

    result = parse_frontmatter_file(path)
    meta = result.metadata

    # Extract variables from prompt ({{var}} pattern).
    variables = tuple(re.findall(r"\{\{(\w+)\}\}", result.body))

    return Template(
        name=str(meta.get("name", name)),
        description=str(meta.get("description", "")),
        prompt=result.body.strip(),
        variables=variables,
        allowed_tools=tuple(meta.get("allowed_tools", [])),
        source_path=str(path),
    )


def list_templates() -> list[Template]:
    """List all available templates."""
    if not _TEMPLATES_DIR.is_dir():
        return []
    templates: list[Template] = []
    for path in sorted(_TEMPLATES_DIR.glob("*.md")):
        try:
            tmpl = load_template(path.stem)
            if tmpl is not None:
                templates.append(tmpl)
        except Exception:
            logger.debug("Failed to load template: %s", path, exc_info=True)
    return templates


def create_template(
    name: str,
    description: str,
    prompt: str,
    *,
    allowed_tools: list[str] | None = None,
) -> Path:
    """Create a new template file."""
    _TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    path = _TEMPLATES_DIR / f"{name}.md"

    frontmatter_lines = [
        "+++",
        f'name = "{name}"',
        f'description = "{description}"',
    ]
    if allowed_tools:
        tools_str = ", ".join(f'"{t}"' for t in allowed_tools)
        frontmatter_lines.append(f"allowed_tools = [{tools_str}]")
    frontmatter_lines.append("+++")
    frontmatter_lines.append("")

    content = "\n".join(frontmatter_lines) + prompt + "\n"
    path.write_text(content, encoding="utf-8")
    return path
