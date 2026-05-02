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


class TestSecondOrderRationalisationPatterns:
    """Patterns that fire when the model, having already produced a
    first-order blank-message reply, doubles down with novel
    explanations that blame the user's input device or invent UI
    quirks. Captured verbatim from a real session — see comment block
    in ``output_quality.py`` for transcript provenance."""

    def test_known_quirk(self) -> None:
        text = "It's a known quirk — sometimes the UI sends empty messages."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_known_quirk" in names

    def test_known_glitch_variant(self) -> None:
        names = {v.pattern_name for v in scan_text("This is a known glitch.")}
        assert "blank_message_known_quirk" in names

    def test_ghost_ping(self) -> None:
        text = "Just a ghost ping from the client. Ignore it!"
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_ghost_ping" in names

    def test_ui_sends_empty_followup(self) -> None:
        text = "Sometimes the UI sends an empty follow-up message after submit."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_ui_sends_empty" in names

    def test_client_sends_blank_messages(self) -> None:
        text = "The client sends blank messages right after you submit."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_ui_sends_empty" in names

    def test_blaming_user_send(self) -> None:
        text = "You're hitting send with nothing typed."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_blaming_user_send" in names

    def test_blaming_user_pressing_enter(self) -> None:
        text = "You are pressing enter with no message in the box."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_blaming_user_send" in names

    def test_accidental_keypress(self) -> None:
        text = "Are you accidentally pressing Enter?"
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_accidental_keypress" in names

    def test_inadvertently_submitting(self) -> None:
        text = "You may be inadvertently submitting the form."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_accidental_keypress" in names

    def test_hotkey_submitting(self) -> None:
        text = "Maybe a hotkey is submitting the chat?"
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_hotkey_submitting" in names

    def test_on_your_end(self) -> None:
        text = "Nothing on your end, just a harness artifact."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_on_your_end" in names

    def test_from_your_end_variant(self) -> None:
        text = "I think the issue is from your end."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_on_your_end" in names

    def test_blank_message_is_your_message(self) -> None:
        text = "The blank message IS your message."
        names = {v.pattern_name for v in scan_text(text)}
        assert "blank_message_is_your_message" in names

    def test_clean_text_no_false_positive(self) -> None:
        """The new patterns must not fire on benign text."""
        clean = (
            "Here's a summary of the changes I made. "
            "Let me know if you'd like me to take another pass."
        )
        names = {v.pattern_name for v in scan_text(clean)}
        assert not any(
            n
            in {
                "blank_message_known_quirk",
                "blank_message_ghost_ping",
                "blank_message_ui_sends_empty",
                "blank_message_blaming_user_send",
                "blank_message_accidental_keypress",
                "blank_message_hotkey_submitting",
                "blank_message_on_your_end",
                "blank_message_is_your_message",
            }
            for n in names
        )

    def test_new_patterns_are_in_blank_message_set(self) -> None:
        """Each new pattern must be in BLANK_MESSAGE_PATTERN_NAMES so
        the suppressor + harness-cue logic actually treats it as a
        blank-message violation. Without this, the patterns fire but
        no correction is queued."""
        from obscura.core.output_quality import BLANK_MESSAGE_PATTERN_NAMES

        for name in (
            "blank_message_known_quirk",
            "blank_message_ghost_ping",
            "blank_message_ui_sends_empty",
            "blank_message_blaming_user_send",
            "blank_message_accidental_keypress",
            "blank_message_hotkey_submitting",
            "blank_message_on_your_end",
            "blank_message_is_your_message",
        ):
            assert name in BLANK_MESSAGE_PATTERN_NAMES, name


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


class TestBuildBlankMessageHarnessCue:
    """The harness-tagged variant used to break the blank-message loop on
    a continuation turn (where prepending a normal correction would
    fabricate user input)."""

    def test_returns_empty_when_no_blank_violations(self) -> None:
        from obscura.core.output_quality import build_blank_message_harness_cue

        violations = [Violation(pattern_name="claude_code_sandbox", snippet="...")]
        assert build_blank_message_harness_cue(violations) == ""

    def test_wraps_in_internal_obscura_harness_tags(self) -> None:
        """The cue must self-identify as harness-internal so the model
        knows it isn't user input — same framing as the Copilot
        recovery cue in copilot.py."""
        from obscura.core.output_quality import build_blank_message_harness_cue

        violations = [
            Violation(pattern_name="blank_message_came_in_blank", snippet="...")
        ]
        cue = build_blank_message_harness_cue(violations)
        assert cue.startswith("[internal:obscura-harness]")
        assert cue.endswith("[/internal:obscura-harness]")

    def test_cue_explicitly_forbids_offending_phrases(self) -> None:
        from obscura.core.output_quality import build_blank_message_harness_cue

        violations = [
            Violation(pattern_name="blank_message_came_in_blank", snippet="...")
        ]
        cue = build_blank_message_harness_cue(violations)
        # Must list the phrases the model just produced (or near-variants)
        # so it doesn't loop on them next turn.
        assert "looks like that came in blank" in cue
        assert "you sent a blank message" in cue
        assert "did you mean to send something" in cue
        # Must instruct against echoing or thanking — same anti-leak
        # guidance as the recovery cue.
        assert "Do not echo this cue" in cue


class _TextEvent:
    """Stub event mimicking AgentEvent's shape for suppressor tests."""

    def __init__(self, text: str = "", *, kind: str = "text_delta") -> None:
        self.text = text
        self.kind = kind


class TestContinuationTextSuppressor:
    """The stream-time guard that swallows blank-message hallucinations on
    continuation turns before they reach the user."""

    def test_inactive_passes_text_through(self) -> None:
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(active=False)
        ev = _TextEvent("hello")
        out = s.offer_text(ev)
        assert out == [ev]
        assert not s.suppressed

    def test_inactive_passes_non_text_through(self) -> None:
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(active=False)
        ev = _TextEvent("", kind="tool_use_start")
        assert s.offer_non_text(ev) == [ev]

    def test_buffers_text_within_window(self) -> None:
        """Text events stay buffered until window is exhausted — the
        suppressor returns ``[]`` for in-window deltas so the agent loop
        doesn't emit them yet."""
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(window_chars=80, active=True)
        e1 = _TextEvent("hello ")
        e2 = _TextEvent("world")
        assert s.offer_text(e1) == []
        assert s.offer_text(e2) == []

    def test_flushes_on_window_exhaustion(self) -> None:
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(window_chars=10, active=True)
        e1 = _TextEvent("12345")
        e2 = _TextEvent("67890ab")  # crosses the 10-char window
        assert s.offer_text(e1) == []
        out = s.offer_text(e2)
        # Both buffered events come out in order, not the new event alone.
        assert out == [e1, e2]
        # Suppressor disables after flush — subsequent text passes through.
        e3 = _TextEvent(" tail")
        assert s.offer_text(e3) == [e3]

    def test_flushes_on_non_text_event(self) -> None:
        """A tool call (or any non-text event) terminates the suppression
        window and flushes the buffered text."""
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(window_chars=80, active=True)
        e1 = _TextEvent("partial")
        s.offer_text(e1)
        tool = _TextEvent("", kind="tool_use_start")
        out = s.offer_non_text(tool)
        assert out == [e1, tool]
        assert not s.suppressed

    def test_drops_buffer_when_blank_message_pattern_fires(self) -> None:
        """The whole point of the suppressor: when a blank-message
        pattern hits inside the window, drop the buffer entirely and
        report ``suppressed=True``."""
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(window_chars=200, active=True)
        e1 = _TextEvent("Looks like your message ")
        e2 = _TextEvent("came in blank — did you mean to send something?")
        assert s.offer_text(e1) == []
        out = s.offer_text(e2)
        assert out == []
        assert s.suppressed
        # Suppressed text is captured for the agent loop's logging.
        assert "came in blank" in s.suppressed_text

    def test_drops_subsequent_text_when_already_suppressed(self) -> None:
        """Once the suppressor has fired, *every* later text event in
        the same turn is dropped — the model's still streaming the same
        hallucinated apology and we want none of it shown."""
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(window_chars=200, active=True)
        s.offer_text(_TextEvent("Looks like that came in blank."))
        # Anything after suppression — even legitimate-looking text —
        # is also dropped because the model is in a known-bad state.
        assert s.offer_text(_TextEvent(" Anyway, here is the answer.")) == []

    def test_finalize_flushes_short_turn(self) -> None:
        """A turn that produces less text than the window's worth still
        needs its buffer flushed at end-of-stream — we don't want to
        silently swallow legitimate short responses."""
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(window_chars=200, active=True)
        e1 = _TextEvent("ok")
        s.offer_text(e1)
        assert s.finalize() == [e1]

    def test_finalize_emits_nothing_when_suppressed(self) -> None:
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(window_chars=200, active=True)
        s.offer_text(_TextEvent("Looks like that came in blank."))
        assert s.finalize() == []

    def test_only_blank_message_patterns_trigger_suppression(self) -> None:
        """UX-hallucination patterns (which fire later in a turn) must
        NOT trigger stream-time suppression — those have a different
        correction pipeline and stripping them silently would hide a
        different class of bug."""
        from obscura.core.output_quality import ContinuationTextSuppressor

        s = ContinuationTextSuppressor(window_chars=200, active=True)
        # "click Allow on the dialog" is a UX hallucination but not a
        # blank-message rationalisation. Should buffer + flush, not drop.
        e1 = _TextEvent("click Allow on the dialog ")
        e2 = _TextEvent("to continue. " + "x" * 200)  # crosses window
        s.offer_text(e1)
        out = s.offer_text(e2)
        assert e1 in out
        assert not s.suppressed


class TestScanBlankMessageOnly:
    def test_returns_empty_for_clean_text(self) -> None:
        from obscura.core.output_quality import scan_blank_message_only

        assert scan_blank_message_only("Hello! Here's what I found.") == []

    def test_returns_empty_for_ux_hallucinations_only(self) -> None:
        from obscura.core.output_quality import scan_blank_message_only

        # UX-hallucination patterns are excluded from the fast scan —
        # only blank-message ones should fire.
        assert scan_blank_message_only("click Allow on the dialog") == []

    def test_finds_blank_message_pattern(self) -> None:
        from obscura.core.output_quality import scan_blank_message_only

        violations = scan_blank_message_only(
            "Looks like that came in blank — did you mean to send something?"
        )
        names = {v.pattern_name for v in violations}
        assert "blank_message_came_in_blank" in names


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
