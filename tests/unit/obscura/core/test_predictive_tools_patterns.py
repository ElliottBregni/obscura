"""Predictive-tool pattern table is well-typed and matches expected shapes.

The strict-typing pass changed ``_PREDICTION_PATTERNS`` from
``list[tuple[Pattern, str, Any]]`` to a proper alias with a
typed extractor callable. This exercises the public matching
behavior so the typing change can't silently break it.
"""

from __future__ import annotations

import re

from obscura.core.predictive_tools import _PREDICTION_PATTERNS, _init_patterns


def test_init_patterns_populates_table() -> None:
    """Calling ``_init_patterns`` produces a non-empty pattern table."""
    _init_patterns()
    assert len(_PREDICTION_PATTERNS) > 0
    for pattern, tool_name, extractor in _PREDICTION_PATTERNS:
        # The static type is `tuple[re.Pattern[str], str, Callable[...]]`.
        # Validate the runtime shape matches.
        assert isinstance(pattern, re.Pattern)
        assert isinstance(tool_name, str)
        assert callable(extractor)


def test_extractor_returns_dict_str_any() -> None:
    """Each extractor takes a Match and returns a dict[str, Any]."""
    _init_patterns()
    # Pick a pattern that should match a known phrase.
    text = "let me read /tmp/notes.md to understand"
    matched_any = False
    for pattern, _name, extractor in _PREDICTION_PATTERNS:
        m = pattern.search(text)
        if m is not None:
            result = extractor(m)
            assert isinstance(result, dict)
            # Keys must be str (no static-typing escape hatches)
            for key in result:
                assert isinstance(key, str)
            matched_any = True
            break
    assert matched_any, "Expected at least one pattern to match a 'let me read X' phrase"
