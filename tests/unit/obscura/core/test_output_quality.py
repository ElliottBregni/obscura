"""Tests for the hallucinated-UX detector."""

from __future__ import annotations

import logging

import pytest

from obscura.core.output_quality import (
    Violation,
    log_violations,
    scan_text,
)


class TestScanText:
    def test_empty_string(self) -> None:
        assert scan_text("") == []

    def test_clean_text_no_violations(self) -> None:
        text = "Reading the config file. Looks good — three entries."
        assert scan_text(text) == []

    def test_click_allow(self) -> None:
        text = (
            "The tool returned ok but I think you should click Allow on the "
            "dialog before I can continue."
        )
        violations = scan_text(text)
        assert any(v.pattern_name == "claude_code_allow_button" for v in violations)

    def test_press_a_to_allow(self) -> None:
        text = "If you're in the TUI, press `a` to allow when prompted."
        violations = scan_text(text)
        assert any(v.pattern_name == "claude_code_press_a" for v in violations)

    def test_allowed_tools_slash(self) -> None:
        text = "Run /allowed-tools to grant access to the prognostic tools."
        violations = scan_text(text)
        assert any(v.pattern_name == "allowed_tools_slash" for v in violations)

    def test_allowed_tools_flag(self) -> None:
        text = (
            "Restart obscura with claude --allowedTools mcp__obs__prognostic_*"
        )
        violations = scan_text(text)
        assert any(v.pattern_name == "allowed_tools_flag" for v in violations)

    def test_policy_allow(self) -> None:
        text = "In the Obscura CLI, run /policy allow prognostic.*"
        violations = scan_text(text)
        assert any(v.pattern_name == "policy_allow_slash" for v in violations)

    def test_one_time_permission_grant(self) -> None:
        text = (
            "The prognostic tools need a one-time permission grant from you."
        )
        violations = scan_text(text)
        assert any(v.pattern_name == "grant_one_time_permission" for v in violations)

    def test_claude_code_permission_wall(self) -> None:
        text = (
            "This looks like a Claude Code permission layer sitting above "
            "Obscura."
        )
        violations = scan_text(text)
        assert any(v.pattern_name == "claude_code_sandbox" for v in violations)

    def test_approve_in_dialog(self) -> None:
        text = "Please approve the permission dialog when it appears."
        violations = scan_text(text)
        assert any(v.pattern_name == "approve_in_dialog" for v in violations)

    def test_reply_grant(self) -> None:
        text = (
            'or just reply "grant" or "yes" and I\'ll walk you through it'
        )
        violations = scan_text(text)
        assert any(v.pattern_name == "reply_grant_or_yes" for v in violations)

    def test_outer_sandbox_without_claude_code_prefix(self) -> None:
        """The model rephrased 'Claude Code sandbox' to 'outer sandbox' to skirt rule 9."""
        text = (
            "The outer sandbox is still blocking the MCP tool despite "
            "in-session approval."
        )
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "outer_layer" in names
        assert "still_blocking_after_approval" in names
        assert "despite_approval" in names

    def test_alt_tool_path(self) -> None:
        text = "Let me try via the Bash tool path instead."
        violations = scan_text(text)
        assert any(v.pattern_name == "alt_tool_path" for v in violations)

    def test_still_erroring(self) -> None:
        text = "The permission was approved but the tool is still erroring."
        violations = scan_text(text)
        assert any(v.pattern_name == "still_blocking_after_approval" for v in violations)

    def test_full_transcript_finds_multiple(self) -> None:
        """The actual transcript that triggered this work should fire several patterns."""
        text = (
            "The prognostic tools need a one-time permission grant from you. "
            "To approve, run this in your terminal:\n\n"
            "/policy allow mcp__obs__prognostic_*\n\n"
            'Or just reply "grant" or "yes" and I\'ll walk you through it — '
            "once approved I'll pull live market data from both Polymarket "
            "and Kalshi simultaneously."
        )
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        # At least three independent hallucinations — exactly the failure mode.
        assert "grant_one_time_permission" in names
        assert "policy_allow_slash" in names
        assert "reply_grant_or_yes" in names

    def test_snippet_carries_context(self) -> None:
        text = "Bla bla bla please click Allow now please bla bla bla"
        violations = scan_text(text, context_chars=10)
        assert violations
        # Context window means we don't see the very ends.
        assert "click Allow" in violations[0].snippet


class TestLogViolations:
    def test_logs_warning_per_violation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        violations = [
            Violation(pattern_name="x", snippet="snippet a"),
            Violation(pattern_name="y", snippet="snippet b"),
        ]
        with caplog.at_level(logging.WARNING):
            log_violations(violations, turn=3)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        joined = " ".join(r.message for r in warnings)
        assert "snippet a" in joined
        assert "snippet b" in joined
        assert "turn=3" in joined

    def test_no_violations_no_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            log_violations([], turn=1)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 0
