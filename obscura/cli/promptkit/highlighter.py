"""obscura.cli.promptkit.highlighter — input keyword highlighter.

A prompt_toolkit ``Processor`` that scans the input buffer for trigger
keywords (``ultrathink``, ``deep think``, ``think hard``) and applies
per-character gradient style classes so the words glow as the user
types.

Consumers
---------
* ``obscura.cli.promptkit.style.PROMPT_STYLE`` — merges the gradient
  style entries produced by ``_keyword_gradient_styles``.
* ``obscura.cli.promptkit.session_factory.create_prompt_session`` —
  installs ``KeywordHighlighter`` as an input processor.
* ``obscura.cli.prompt`` (legacy back-compat shim).
"""

from __future__ import annotations

from typing import Any, cast, override

from prompt_toolkit.layout.processors import (
    Processor,
    Transformation,
    TransformationInput,
)

# Gradient palette for "ultrathink" (purple to cyan)
_GRADIENT = [
    "#8b5cf6",
    "#7c3aed",
    "#6d28d9",
    "#5b21b6",
    "#4f46e5",
    "#4338ca",
    "#3b82f6",
    "#2563eb",
    "#0ea5e9",
    "#06b6d4",
]

# Keywords that trigger gradient styling, with their style names
_HIGHLIGHT_KEYWORDS: dict[str, str] = {
    "ultrathink": "keyword.ultrathink",
    "deep think": "keyword.deepthink",
    "think hard": "keyword.deepthink",
}


class KeywordHighlighter(Processor):
    """Highlight trigger words in the input with gradient colors.

    Scans the input buffer for keywords like ``ultrathink`` and applies
    per-character style classes that map to gradient colors in the style
    dict.  This makes the keyword glow as you type it.
    """

    @override
    def apply_transformation(
        self,
        transformation_input: TransformationInput,
    ) -> Transformation:
        fragments = transformation_input.fragments
        doc = transformation_input.document
        text = doc.text_before_cursor + doc.text_after_cursor

        if not text:
            return Transformation(fragments)

        # Flatten the existing fragments into a single string to match positions.
        # OneStyleAndTextTuple is (style, text) or (style, text, mouse_handler);
        # index [1] always gives the text part.
        flat = "".join(frag[1] for frag in fragments)
        if not flat:
            return Transformation(fragments)

        # Build a set of character positions that should be styled
        styled_positions: dict[int, str] = {}
        for keyword, _style_name in _HIGHLIGHT_KEYWORDS.items():
            start = 0
            kw_lower = keyword.lower()
            flat_lower = flat.lower()
            while True:
                idx = flat_lower.find(kw_lower, start)
                if idx == -1:
                    break
                for i in range(len(keyword)):
                    # Map each char to a gradient color index
                    gradient_idx = i % len(_GRADIENT)
                    styled_positions[idx + i] = f"class:kw-g{gradient_idx}"
                start = idx + 1

        if not styled_positions:
            return Transformation(fragments)

        # Rebuild fragments with styled characters
        new_fragments: list[tuple[str, str]] = []
        pos = 0
        for frag in fragments:
            style = frag[0]
            segment = frag[1]
            for ch in segment:
                if pos in styled_positions:
                    new_fragments.append((styled_positions[pos], ch))
                else:
                    new_fragments.append((style, ch))
                pos += 1

        # cast: new_fragments is list[tuple[str, str]] which is a valid
        # specialization of StyleAndTextTuples (list[OneStyleAndTextTuple])
        # but pyright doesn't see the OneStyleAndTextTuple union covariantly.
        return Transformation(cast(Any, new_fragments))


def _keyword_gradient_styles() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
    """Build style entries for the gradient character classes."""
    styles: dict[str, str] = {}
    for i, color in enumerate(_GRADIENT):
        styles[f"kw-g{i}"] = f"{color} bold"
    return styles
