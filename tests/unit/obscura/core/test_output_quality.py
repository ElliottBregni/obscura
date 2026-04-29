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


class TestBuildCorrectionPrompt:
    def test_no_violations_returns_empty(self) -> None:
        from obscura.core.output_quality import (
            ToolResultSummary,
            build_correction_prompt,
        )

        result = build_correction_prompt(
            [],
            [ToolResultSummary(tool_name="x", snippet="ok")],
        )
        assert result == ""

    def test_no_successful_tools_returns_empty(self) -> None:
        from obscura.core.output_quality import build_correction_prompt

        result = build_correction_prompt(
            [Violation(pattern_name="x", snippet="..."),],
            [],
        )
        assert result == ""

    def test_builds_correction_with_evidence(self) -> None:
        from obscura.core.output_quality import (
            ToolResultSummary,
            build_correction_prompt,
        )

        violations = [
            Violation(pattern_name="claude_code_sandbox", snippet="..."),
            Violation(pattern_name="still_blocking_after_approval", snippet="..."),
        ]
        tools = [
            ToolResultSummary(
                tool_name="user_interact",
                snippet='{"approved": true}',
            ),
            ToolResultSummary(
                tool_name="prognostic_health_check",
                snippet='{"polymarket": {"rest": "ok"}}',
            ),
        ]
        result = build_correction_prompt(violations, tools)
        # Includes both pattern names so the model sees what it did wrong.
        assert "claude_code_sandbox" in result
        assert "still_blocking_after_approval" in result
        # Includes the actual tool results as ground truth.
        assert "user_interact" in result
        assert "approved" in result
        assert "prognostic_health_check" in result
        # Reinforces the canonical truth.
        assert "OBSCURA CORRECTION" in result
        assert "user_interact" in result


class TestBlankMessagePatterns:
    """Detect the model rationalising real input as blank/empty.

    These phrases pattern-complete from earlier filler in the same
    session and start firing on real user messages too — exactly the
    compound failure mode the agent_loop correction loop exists to
    break.
    """

    def test_came_in_blank(self) -> None:
        text = "Looks like that came in blank — anything else?"
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_came_in_blank" in names

    def test_came_through_empty(self) -> None:
        text = "message came through empty — you good?"
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_came_in_blank" in names

    def test_you_sent_a_blank_message(self) -> None:
        text = "You sent a blank message. What's up?"
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_user_sent_blank" in names

    def test_you_sent_an_empty_message(self) -> None:
        text = "You sent an empty message — did you mean to?"
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_user_sent_blank" in names

    def test_did_you_mean_to_send_something(self) -> None:
        text = "Looks like that came in blank — did you mean to send something?"
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_did_you_mean_send" in names

    def test_another_blank_one(self) -> None:
        text = "another blank one — you sending messages by accident?"
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_another_blank" in names
        assert "blank_message_sending_by_accident" in names

    def test_clean_text_no_false_positive(self) -> None:
        """Real assistant text mentioning 'empty' or 'blank' in legitimate
        contexts (empty list, blank line in code) must NOT fire."""
        text = (
            "The function returns an empty list when no matches are found. "
            "Note the blank line at the end of the file is intentional."
        )
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        # None of the blank-message patterns should fire.
        assert not any(n.startswith("blank_message_") for n in names)

    def test_still_getting_blank_messages(self) -> None:
        """Real session screenshot phrasing — the prompt that triggered
        this whole pattern set being added."""
        text = (
            "Still getting blank messages from you — might be a copy-paste "
            "issue on your end. Try again when ready!"
        )
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_still_getting" in names
        assert "blank_message_copy_paste_issue" in names

    def test_getting_empty_messages_variant(self) -> None:
        text = "I'm getting empty messages — anything I should retry?"
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_still_getting" in names

    def test_copy_paste_glitch_variant(self) -> None:
        text = "looks like a copy paste glitch on your end"
        violations = scan_text(text)
        names = {v.pattern_name for v in violations}
        assert "blank_message_copy_paste_issue" in names


class TestBuildBlankMessageCorrection:
    def test_returns_empty_when_no_blank_violations(self) -> None:
        from obscura.core.output_quality import build_blank_message_correction

        # UX-hallucination violations alone should not trigger a blank
        # correction.
        violations = [Violation(pattern_name="claude_code_sandbox", snippet="...")]
        assert build_blank_message_correction(violations) == ""

    def test_builds_correction_for_blank_violation(self) -> None:
        from obscura.core.output_quality import build_blank_message_correction

        violations = [
            Violation(pattern_name="blank_message_came_in_blank", snippet="..."),
            Violation(
                pattern_name="blank_message_did_you_mean_send", snippet="..."
            ),
        ]
        result = build_blank_message_correction(violations)
        assert "OBSCURA CORRECTION" in result
        assert "blank_message_came_in_blank" in result
        assert "blank_message_did_you_mean_send" in result
        # The correction must explicitly forbid the offending phrases so
        # the next turn doesn't just reproduce them.
        assert "looks like that came in blank" in result
        assert "you sent a blank message" in result
        # And must point at the harness as the actual source of any
        # apparent emptiness, NOT the user.
        assert "harness artifact" in result

    def test_no_correction_when_only_ux_hallucinations(self) -> None:
        """A UX-hallucination violation alone shouldn't trigger the
        blank-message correction (different category)."""
        from obscura.core.output_quality import (
            build_blank_message_correction,
            has_blank_message_violation,
        )

        violations = [
            Violation(pattern_name="claude_code_allow_button", snippet="..."),
            Violation(pattern_name="allowed_tools_slash", snippet="..."),
        ]
        assert not has_blank_message_violation(violations)
        assert build_blank_message_correction(violations) == ""


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
