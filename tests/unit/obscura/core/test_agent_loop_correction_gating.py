"""Tests for ``AgentLoop._consume_pending_correction``.

Hardens the hallucination-correction pipeline so it only injects on real
user-driven turns. The original behaviour eagerly prepended the
correction onto ``current_prompt`` regardless of what that prompt was —
including:

* empty post-tool continuations (``current_prompt = ""`` after a tool
  call, the agent loop's normal continuation pattern); and
* Copilot recovery primers (``[internal:obscura-harness] ...``).

In both cases the next turn isn't user-driven, so prepending a
``[OBSCURA CORRECTION]`` block fakes a user message that the model
treats as instruction. The gating holds the correction across those
iterations and consumes it on the next real user prompt — or drops it
after ``_PENDING_CORRECTION_TTL`` without one.
"""

from __future__ import annotations

from obscura.core.agent_loop import AgentLoop
from obscura.core.tools import ToolRegistry


def _loop() -> AgentLoop:
    return AgentLoop(backend=None, tool_registry=ToolRegistry())


class TestConsumePendingCorrection:
    def test_no_pending_correction_passthrough(self) -> None:
        loop = _loop()
        new_prompt, emitted = loop._consume_pending_correction("hello")
        assert new_prompt == "hello"
        assert emitted is None

    def test_real_user_prompt_consumes_and_prepends(self) -> None:
        loop = _loop()
        loop._pending_correction = "[OBSCURA CORRECTION] stop saying blank"

        new_prompt, emitted = loop._consume_pending_correction("now do this")

        assert new_prompt.startswith("[OBSCURA CORRECTION]")
        assert new_prompt.endswith("now do this")
        assert emitted == "[OBSCURA CORRECTION] stop saying blank"
        # State cleared so it doesn't fire again on the next real turn.
        assert loop._pending_correction is None
        assert loop._pending_correction_age == 0

    def test_empty_continuation_holds_correction(self) -> None:
        """``current_prompt = ""`` is the agent loop's post-tool turn —
        prepending a correction here would fabricate a user message."""
        loop = _loop()
        loop._pending_correction = "[OBSCURA CORRECTION]"

        new_prompt, emitted = loop._consume_pending_correction("")

        assert new_prompt == ""
        assert emitted is None
        # Correction must still be queued for the *next* real user prompt.
        assert loop._pending_correction == "[OBSCURA CORRECTION]"
        assert loop._pending_correction_age == 1

    def test_whitespace_only_treated_as_continuation(self) -> None:
        loop = _loop()
        loop._pending_correction = "x"
        _, emitted = loop._consume_pending_correction("   \n\t  ")
        assert emitted is None
        assert loop._pending_correction == "x"

    def test_internal_harness_cue_holds_correction(self) -> None:
        """The Copilot recovery cue is harness-internal, not a user
        prompt. Layering a correction onto it would just nest harness
        directives."""
        loop = _loop()
        loop._pending_correction = "[OBSCURA CORRECTION]"
        cue = (
            "[internal:obscura-harness] The agent harness sent this — "
            "the user did NOT type it."
        )

        new_prompt, emitted = loop._consume_pending_correction(cue)

        assert new_prompt == cue  # unmodified
        assert emitted is None
        assert loop._pending_correction == "[OBSCURA CORRECTION]"

    def test_correction_held_then_consumed_on_real_prompt(self) -> None:
        """End-to-end: hold across a continuation, then fire on the next
        user turn that does come."""
        loop = _loop()
        loop._pending_correction = "[CORR]"

        # Three continuations — held each time.
        for _ in range(3):
            prompt, emitted = loop._consume_pending_correction("")
            assert emitted is None
            assert loop._pending_correction == "[CORR]"

        # Real user prompt — fires.
        prompt, emitted = loop._consume_pending_correction("real input")
        assert emitted == "[CORR]"
        assert prompt.startswith("[CORR]\n\n")
        assert prompt.endswith("real input")

    def test_dropped_after_ttl(self) -> None:
        loop = _loop()
        loop._pending_correction = "x"
        ttl = AgentLoop._PENDING_CORRECTION_TTL

        # First TTL holds keep the correction queued.
        for i in range(ttl):
            _, emitted = loop._consume_pending_correction("")
            assert emitted is None
            assert loop._pending_correction == "x", f"dropped early at hold {i}"

        # The TTL+1-th hold drops it. A correction tied to a stale
        # hallucination has no reason to outlive its turn budget.
        _, emitted = loop._consume_pending_correction("")
        assert emitted is None
        assert loop._pending_correction is None
        assert loop._pending_correction_age == 0

    def test_fresh_correction_resets_age(self) -> None:
        """If a correction was held a few turns then a NEW correction
        replaces it (because the model re-hallucinated), the new one
        starts with a fresh TTL budget."""
        loop = _loop()
        loop._pending_correction = "old"
        # Hold it once.
        loop._consume_pending_correction("")
        assert loop._pending_correction_age == 1

        # New correction queued (simulating what scan_text + assignment
        # at the end of a turn does).
        loop._pending_correction = "new"
        loop._pending_correction_age = 0  # done by the assignment site

        # Now we should be able to hold it ttl more times before drop.
        ttl = AgentLoop._PENDING_CORRECTION_TTL
        for _ in range(ttl):
            _, emitted = loop._consume_pending_correction("")
            assert emitted is None
            assert loop._pending_correction == "new"

    def test_internal_cue_with_leading_whitespace(self) -> None:
        """The harness-cue check is lstrip-tolerant so inadvertent
        leading whitespace on the primer doesn't bypass the gate."""
        loop = _loop()
        loop._pending_correction = "x"
        prompt = "   [internal:obscura-harness] continue"

        _, emitted = loop._consume_pending_correction(prompt)

        assert emitted is None
        assert loop._pending_correction == "x"
