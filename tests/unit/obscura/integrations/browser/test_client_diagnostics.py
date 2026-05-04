"""Unit tests for browser-bridge error diagnostics.

The proxy handler in ``obscura.integrations.browser.client`` wraps every
``BrowserBridgeError`` raised by a browser tool with an action hint based
on the error message, so the LLM agent receives both the tool name and a
remediation suggestion instead of a bare technical string.

These tests cover the pure helper only — no socket, no event loop.
"""

from __future__ import annotations

import pytest

from obscura.integrations.browser.client import _diagnostic_for_error


class TestDiagnosticForError:
    def test_includes_tool_name_when_provided(self) -> None:
        out = _diagnostic_for_error("browser_click", "no match")
        assert out.startswith("browser_click: ")
        assert "no match" in out

    def test_omits_prefix_when_tool_name_empty(self) -> None:
        out = _diagnostic_for_error("", "no match")
        assert not out.startswith(": ")
        assert "no match" in out

    def test_no_match_appends_verify_hint(self) -> None:
        out = _diagnostic_for_error("browser_fill", "no match")
        assert "verify" in out.lower() or "screenshot" in out.lower()

    def test_timeout_after_n_seconds_hint_mentions_state_check(self) -> None:
        out = _diagnostic_for_error(
            "browser_click",
            "browser bridge call 'browser_click' timed out after 60.0s",
        )
        assert "timed out" in out.lower()
        assert "browser_screenshot" in out or "browser_read_page" in out

    def test_socket_unreachable_hint_mentions_side_panel(self) -> None:
        out = _diagnostic_for_error(
            "browser_click",
            "socket /tmp/foo.sock not reachable: [Errno 2]",
        )
        assert "side panel" in out.lower()

    def test_bridge_closed_hint_mentions_reopening(self) -> None:
        out = _diagnostic_for_error("browser_fill", "bridge is closed")
        assert "re-open" in out.lower() or "reopen" in out.lower()

    def test_bridge_connection_closed_hint_mentions_disconnect(self) -> None:
        out = _diagnostic_for_error(
            "browser_native_click", "bridge connection closed"
        )
        assert "disconnect" in out.lower() or "side panel" in out.lower()

    def test_debugger_already_attached_hint_mentions_devtools(self) -> None:
        out = _diagnostic_for_error(
            "browser_type_text",
            "debugger already attached to this target",
        )
        assert "devtools" in out.lower() or "another" in out.lower()

    def test_no_active_tab_hint_mentions_tab_focus(self) -> None:
        out = _diagnostic_for_error("browser_click", "no active tab")
        assert "tab" in out.lower()

    def test_unknown_error_still_returns_message_with_prefix(self) -> None:
        out = _diagnostic_for_error("browser_click", "wat is this")
        assert out == "browser_click: wat is this"

    def test_empty_message_falls_back_to_unknown(self) -> None:
        out = _diagnostic_for_error("browser_click", "")
        assert "unknown error" in out

    @pytest.mark.parametrize(
        "raw,must_contain",
        [
            ("no match", "verify"),
            ("timed out after 30.0s", "screenshot"),
            ("bridge is closed", "re-open"),
            ("debugger already attached", "devtools"),
        ],
    )
    def test_pattern_table(self, raw: str, must_contain: str) -> None:
        out = _diagnostic_for_error("tool_x", raw).lower()
        assert must_contain in out
