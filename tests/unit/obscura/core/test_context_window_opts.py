"""Tests for context window optimizations (Changes 3 and 7).

Change 3: SNIP_TOOL_OUTPUT_THRESHOLD lowered from 10_000 to 3_000
Change 7: should_auto_compact threshold lowered from 0.80 to 0.70
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Change 3: snip threshold
# ---------------------------------------------------------------------------


def test_snip_threshold_default_is_3000() -> None:
    """SNIP_TOOL_OUTPUT_THRESHOLD must default to 3_000 (down from 10_000)."""
    import importlib

    # Reload to pick up fresh env (no override set in this test).
    import obscura.core.context_window as cw

    importlib.reload(cw)
    assert cw.SNIP_TOOL_OUTPUT_THRESHOLD == 3_000


def test_snip_threshold_env_override() -> None:
    """OBSCURA_SNIP_THRESHOLD env var overrides the default."""
    import importlib
    import os

    with patch.dict(os.environ, {"OBSCURA_SNIP_THRESHOLD": "5000"}):
        import obscura.core.context_window as cw

        importlib.reload(cw)
        assert cw.SNIP_TOOL_OUTPUT_THRESHOLD == 5_000
    # Reload back to the default so other tests aren't affected.
    importlib.reload(cw)


def _make_tool_result_message(text: str) -> dict[str, Any]:
    # Use "text" key directly — that's what _get_block_text() reads.
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu-1",
                "text": text,
            }
        ],
    }


def test_snip_tool_outputs_snips_above_3000_tokens() -> None:
    """A tool result exceeding 3_000 estimated tokens should be snipped."""
    from obscura.core.compaction import snip_tool_outputs

    # ~4000 words ≈ ~5000 tokens (well above 3_000)
    large_text = "word " * 4000
    messages = [_make_tool_result_message(large_text)]

    result, snipped_count, tokens_freed = snip_tool_outputs(messages, threshold_tokens=3_000)
    assert snipped_count == 1
    assert tokens_freed > 0
    # The result text should contain the snip marker.
    result_block = result[0]["content"][0]
    text_out = result_block.get("text", "")
    assert "[snipped:" in text_out, f"Expected [snipped:] in output, got: {text_out[:200]!r}"


def test_snip_tool_outputs_keeps_small_result_intact() -> None:
    """A tool result under 3_000 tokens must NOT be snipped."""
    from obscura.core.compaction import snip_tool_outputs

    # ~100 words ≈ ~130 tokens — well under 3_000
    small_text = "word " * 100
    messages = [_make_tool_result_message(small_text)]

    result, snipped_count, tokens_freed = snip_tool_outputs(messages, threshold_tokens=3_000)
    assert snipped_count == 0
    assert tokens_freed == 0


# ---------------------------------------------------------------------------
# Change 7: compaction trigger threshold
# ---------------------------------------------------------------------------


def _fake_messages(n: int) -> list[dict[str, Any]]:
    """Build n short messages for token estimation."""
    return [{"role": "user", "content": "hello"} for _ in range(n)]


def test_should_auto_compact_default_threshold_is_70_pct() -> None:
    """Default threshold must be 0.70 (down from 0.80)."""
    import inspect

    from obscura.core.compaction import should_auto_compact

    sig = inspect.signature(should_auto_compact)
    default = sig.parameters["threshold"].default
    assert default == 0.70, f"Expected 0.70, got {default}"


def _make_user_msg(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def test_should_auto_compact_triggers_at_71_pct() -> None:
    """should_auto_compact returns True when usage > 70%."""
    from obscura.core.compaction import should_auto_compact
    from obscura.core.context_window import estimate_tokens, get_context_window

    model = "gpt-4"  # small 8192-token window — easier to fill
    window = get_context_window(model)
    # Fill 72% of the window via a single large message.
    target_tokens = int(window * 0.72)
    words_needed = int(target_tokens * 0.75) + 100
    msg_text = "token " * words_needed

    actual_tokens = estimate_tokens(msg_text)
    assert actual_tokens >= int(window * 0.70), (
        f"Setup error: message has {actual_tokens} tokens, need >= {int(window * 0.70)}"
    )

    messages = [_make_user_msg(msg_text)]
    result = should_auto_compact(messages, model, threshold=0.70)
    assert result is True


def test_should_auto_compact_does_not_trigger_at_50_pct() -> None:
    """should_auto_compact returns False when usage is well below 70%."""
    from obscura.core.compaction import should_auto_compact
    from obscura.core.context_window import estimate_tokens, get_context_window

    model = "gpt-4"
    window = get_context_window(model)
    # Fill ~50% — safely under 70%.
    words_needed = int(window * 0.50 * 0.75)
    msg_text = "token " * words_needed
    actual_tokens = estimate_tokens(msg_text)
    assert actual_tokens < int(window * 0.70), (
        f"Setup error: message has {actual_tokens} tokens, should be < {int(window * 0.70)}"
    )

    messages = [_make_user_msg(msg_text)]
    result = should_auto_compact(messages, model, threshold=0.70)
    assert result is False


def test_should_auto_compact_respects_custom_threshold() -> None:
    """Explicit threshold kwarg overrides the default."""
    from obscura.core.compaction import should_auto_compact
    from obscura.core.context_window import estimate_tokens, get_context_window

    model = "gpt-4"
    window = get_context_window(model)
    # Build ~75% fill.
    words_needed = int(window * 0.75 * 0.75) + 100
    msg_text = "token " * words_needed
    actual_tokens = estimate_tokens(msg_text)

    # Sanity: must be between 70% and 80% of context window.
    assert int(window * 0.70) <= actual_tokens <= window, (
        f"Setup error: {actual_tokens} tokens not in expected range"
    )

    messages = [_make_user_msg(msg_text)]

    # With 0.80 threshold: should NOT trigger if usage < 80%
    if actual_tokens < int(window * 0.80):
        assert should_auto_compact(messages, model, threshold=0.80) is False

    # With 0.70 threshold: SHOULD trigger (usage > 70%)
    assert should_auto_compact(messages, model, threshold=0.70) is True
