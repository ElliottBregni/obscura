"""Tests for context window optimizations in system prompts (Change 6).

Change 6: Lazy-load browser/delegation prompt sections.
- compose_system_prompt gains include_browser_tools param
- When False, the ## Browser Tools section is absent
- When True, the section is present
- Auto-detect uses _is_browser_running()
"""

from __future__ import annotations

from unittest.mock import patch


def _default_prompt_has_browser_section() -> bool:
    """Check that the raw default prompt has the browser section."""
    from obscura.core.system_prompts import (
        _BROWSER_SECTION_MARKER,
        get_default_system_prompt,
    )

    return _BROWSER_SECTION_MARKER in get_default_system_prompt()


def test_browser_section_absent_when_disabled() -> None:
    """compose_system_prompt with include_browser_tools=False omits browser section."""
    from obscura.core.system_prompts import (
        _BROWSER_SECTION_MARKER,
        compose_system_prompt,
    )

    # Only meaningful if the default prompt has the section.
    if not _default_prompt_has_browser_section():
        return

    result = compose_system_prompt(include_browser_tools=False)
    assert _BROWSER_SECTION_MARKER not in result


def test_browser_section_present_when_enabled() -> None:
    """compose_system_prompt with include_browser_tools=True includes browser section."""
    from obscura.core.system_prompts import (
        _BROWSER_SECTION_MARKER,
        compose_system_prompt,
    )

    if not _default_prompt_has_browser_section():
        return

    result = compose_system_prompt(include_browser_tools=True)
    assert _BROWSER_SECTION_MARKER in result


def test_browser_section_auto_detect_no_browser() -> None:
    """When no browser host is running, browser section is omitted by default."""
    from obscura.core.system_prompts import (
        _BROWSER_SECTION_MARKER,
        compose_system_prompt,
    )

    if not _default_prompt_has_browser_section():
        return

    with patch("obscura.core.system_prompts._is_browser_running", return_value=False):
        result = compose_system_prompt()
    assert _BROWSER_SECTION_MARKER not in result


def test_browser_section_auto_detect_browser_running() -> None:
    """When a browser host is detected, browser section is included by default."""
    from obscura.core.system_prompts import (
        _BROWSER_SECTION_MARKER,
        compose_system_prompt,
    )

    if not _default_prompt_has_browser_section():
        return

    with patch("obscura.core.system_prompts._is_browser_running", return_value=True):
        result = compose_system_prompt()
    assert _BROWSER_SECTION_MARKER in result


def test_split_browser_section_no_marker() -> None:
    """_split_browser_section on a prompt with no marker returns (prompt, '')."""
    from obscura.core.system_prompts import _split_browser_section

    prompt = "Hello world\nNo browser section here."
    body, browser = _split_browser_section(prompt)
    assert body == prompt
    assert browser == ""


def test_split_browser_section_with_marker() -> None:
    """_split_browser_section correctly splits at the marker."""
    from obscura.core.system_prompts import (
        _BROWSER_SECTION_MARKER,
        _split_browser_section,
    )

    main = "Some content here."
    browser_part = f"{_BROWSER_SECTION_MARKER}\nUse browser_click etc."
    prompt = f"{main}\n\n{browser_part}"
    body, browser = _split_browser_section(prompt)
    assert _BROWSER_SECTION_MARKER not in body
    assert browser.startswith(_BROWSER_SECTION_MARKER)
