"""Tests for obscura.core.context_window and obscura.core.compaction."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestEstimateTokens:
    def test_empty(self) -> None:
        from obscura.core.context_window import estimate_tokens

        assert estimate_tokens("") == 0

    def test_short_text(self) -> None:
        from obscura.core.context_window import estimate_tokens

        assert 1 <= estimate_tokens("hello world") <= 10

    def test_longer_proportional(self) -> None:
        from obscura.core.context_window import estimate_tokens

        assert estimate_tokens("hello world foo bar baz qux") > estimate_tokens("hello")

    def test_code_block(self) -> None:
        from obscura.core.context_window import estimate_tokens

        assert estimate_tokens("def foo():\n    return 42\n") > 0


class TestGetContextWindow:
    def test_exact_claude(self) -> None:
        from obscura.core.context_window import get_context_window

        assert get_context_window("claude-opus-4-5") == 200_000

    def test_exact_openai(self) -> None:
        from obscura.core.context_window import get_context_window

        assert get_context_window("gpt-4o") == 128_000

    def test_exact_gemini(self) -> None:
        from obscura.core.context_window import get_context_window

        assert get_context_window("gemini-2.0-flash") == 1_000_000

    def test_prefix_match(self) -> None:
        from obscura.core.context_window import get_context_window

        assert get_context_window("claude-sonnet-4-5-20241022") == 200_000

    def test_unknown_returns_default(self) -> None:
        from obscura.core.context_window import get_context_window

        assert get_context_window("totally-unknown-xyz") == 100_000


class TestEstimateMessagesTokens:
    def test_empty_list(self) -> None:
        from obscura.core.context_window import estimate_messages_tokens

        assert estimate_messages_tokens([]) == 0

    def test_overhead_per_message(self) -> None:
        from obscura.core.context_window import estimate_messages_tokens

        # empty content = 0 tokens + 4 overhead
        assert estimate_messages_tokens([{"role": "user", "content": ""}]) == 4

    def test_two_messages_more_than_one(self) -> None:
        from obscura.core.context_window import estimate_messages_tokens

        one = estimate_messages_tokens([{"role": "user", "content": "hello"}])
        two = estimate_messages_tokens(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ],
        )
        assert two > one

    def test_list_content(self) -> None:
        from obscura.core.context_window import estimate_messages_tokens

        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me help."},
                    {"type": "tool_use", "id": "tu_1", "name": "read", "input": {}},
                ],
            },
        ]
        assert estimate_messages_tokens(msgs) > 0


class TestEvaluateContextStatus:
    def test_ok_on_small_history(self) -> None:
        from obscura.core.context_window import evaluate_context_status

        status = evaluate_context_status(
            [{"role": "user", "content": "hi"}],
            "claude-opus-4-5",
        )
        assert not status.should_warn
        assert not status.should_block

    def test_warn_threshold(self) -> None:
        from obscura.core.context_window import evaluate_context_status

        with patch(
            "obscura.core.context_window.estimate_messages_tokens",
            return_value=200_000 - 30_000 - 4096,
        ):
            s = evaluate_context_status([], "claude-opus-4-5")
            assert s.should_warn
            assert not s.should_block

    def test_block_threshold(self) -> None:
        from obscura.core.context_window import evaluate_context_status

        with patch(
            "obscura.core.context_window.estimate_messages_tokens",
            return_value=200_000 - 10_000 - 4096,
        ):
            s = evaluate_context_status([], "claude-opus-4-5")
            assert s.should_block

    def test_str_repr(self) -> None:
        from obscura.core.context_window import evaluate_context_status

        s = evaluate_context_status([{"role": "user", "content": "hi"}], "gpt-4o")
        assert any(x in str(s) for x in ["[OK]", "[WARN]", "[BLOCK]"])


class TestRepairToolPairs:
    def test_all_paired_unchanged(self) -> None:
        from obscura.core.compaction import repair_tool_pairs

        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "f", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
                ],
            },
        ]
        assert len(repair_tool_pairs(msgs)) == 2

    def test_removes_orphaned(self) -> None:
        from obscura.core.compaction import repair_tool_pairs

        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_gone", "content": "x"},
                ],
            },
            {"role": "assistant", "content": "Hello"},
        ]
        result = repair_tool_pairs(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"

    def test_partial_repair(self) -> None:
        from obscura.core.compaction import repair_tool_pairs

        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_good", "name": "r", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_good",
                        "content": "data",
                    },
                    {"type": "tool_result", "tool_use_id": "tu_gone", "content": "x"},
                ],
            },
        ]
        result = repair_tool_pairs(msgs)
        assert len(result) == 2
        assert len(result[1]["content"]) == 1

    def test_empty_list(self) -> None:
        from obscura.core.compaction import repair_tool_pairs

        assert repair_tool_pairs([]) == []


class TestCompactHistory:
    def _backend(self, summary="Summary."):
        b = MagicMock()
        b.complete = AsyncMock(return_value=summary)
        return b

    def _msgs(self, n, big=False):
        w = "word " * (500 if big else 10)
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Msg {i}: {w}"}
            for i in range(n)
        ]

    def test_empty(self) -> None:
        from obscura.core.compaction import compact_history

        r, d, _mems = asyncio.get_event_loop().run_until_complete(
            compact_history([], "claude-opus-4-5", self._backend()),
        )
        assert r == []
        assert not d

    def test_small_no_compact(self) -> None:
        from obscura.core.compaction import compact_history

        msgs = self._msgs(5)
        _, did, _mems = asyncio.get_event_loop().run_until_complete(
            compact_history(msgs, "claude-opus-4-5", self._backend()),
        )
        assert not did

    def test_compaction_reduces(self) -> None:
        from obscura.core.compaction import compact_history

        msgs = self._msgs(50)
        with patch("obscura.core.compaction.get_context_window", return_value=1000):
            result, did, _mems = asyncio.get_event_loop().run_until_complete(
                compact_history(msgs, "tiny", self._backend()),
            )
        assert did
        assert len(result) < len(msgs)

    def test_no_orphaned_tool_results_after_compact(self) -> None:
        from obscura.core.compaction import compact_history

        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_old", "name": "r", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_old", "content": "data"},
                ],
            },
            *self._msgs(30),
        ]
        with patch("obscura.core.compaction.get_context_window", return_value=500):
            result, _, _mems = asyncio.get_event_loop().run_until_complete(
                compact_history(msgs, "tiny", self._backend()),
            )
        tool_use_ids = {
            b["id"]
            for m in result
            for b in (m.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "tool_use"
        }
        for m in result:
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    assert b["tool_use_id"] in tool_use_ids


class TestAdaptiveChunkRatio:
    def test_normal_uses_base(self) -> None:
        from obscura.core.compaction import _compute_adaptive_chunk_ratio

        msgs = [{"role": "user", "content": "hi"}]
        assert _compute_adaptive_chunk_ratio(msgs, 200_000) == 0.4

    def test_large_msgs_reduce_ratio(self) -> None:
        from obscura.core.compaction import _compute_adaptive_chunk_ratio

        msgs = [{"role": "user", "content": "word " * 3000}]
        ratio = _compute_adaptive_chunk_ratio(msgs, 20_000)
        assert ratio < 0.4

    def test_floor_at_min(self) -> None:
        from obscura.core.compaction import (
            MIN_CHUNK_RATIO,
            _compute_adaptive_chunk_ratio,
        )

        msgs = [{"role": "user", "content": "word " * 100_000}]
        assert _compute_adaptive_chunk_ratio(msgs, 10_000) >= MIN_CHUNK_RATIO


# ---------------------------------------------------------------------------
# CompactThresholds tests
# ---------------------------------------------------------------------------


class TestCompactThresholds:
    def test_large_model_thresholds(self) -> None:
        from obscura.core.context_window import get_compact_thresholds

        t = get_compact_thresholds("claude-opus-4-5")
        assert t.context_window == 200_000
        assert t.snip_at < t.compact_at < t.critical_at
        assert t.preserve_recent == 6  # large profile

    def test_medium_model_thresholds(self) -> None:
        from obscura.core.context_window import get_compact_thresholds

        t = get_compact_thresholds("gpt-4o")
        assert t.context_window == 128_000
        assert t.preserve_recent == 4  # medium profile

    def test_small_model_thresholds(self) -> None:
        from obscura.core.context_window import get_compact_thresholds

        t = get_compact_thresholds("gpt-4")
        assert t.context_window == 8_192
        assert t.preserve_recent == 3  # small profile

    def test_usage_tier_ok(self) -> None:
        from obscura.core.context_window import get_compact_thresholds

        t = get_compact_thresholds("claude-opus-4-5")
        assert t.usage_tier(1000) == "ok"

    def test_usage_tier_snip(self) -> None:
        from obscura.core.context_window import get_compact_thresholds

        t = get_compact_thresholds("claude-opus-4-5")
        # snip_at = 200K * 0.60 = 120K
        assert t.usage_tier(125_000) == "snip"

    def test_usage_tier_compact(self) -> None:
        from obscura.core.context_window import get_compact_thresholds

        t = get_compact_thresholds("claude-opus-4-5")
        # compact_at = 200K * 0.75 = 150K
        assert t.usage_tier(155_000) == "compact"

    def test_usage_tier_critical(self) -> None:
        from obscura.core.context_window import get_compact_thresholds

        t = get_compact_thresholds("claude-opus-4-5")
        # critical_at = 200K * 0.90 = 180K
        assert t.usage_tier(185_000) == "critical"

    def test_context_status_includes_tier(self) -> None:
        from obscura.core.context_window import evaluate_context_status

        s = evaluate_context_status(
            [{"role": "user", "content": "hi"}], "claude-opus-4-5"
        )
        assert s.compact_tier == "ok"

    def test_context_status_tier_in_str(self) -> None:
        from obscura.core.context_window import evaluate_context_status

        with patch(
            "obscura.core.context_window.estimate_messages_tokens",
            return_value=155_000,
        ):
            s = evaluate_context_status([], "claude-opus-4-5")
            assert "tier=" in str(s)


# ---------------------------------------------------------------------------
# Snip compact tests
# ---------------------------------------------------------------------------


class TestSnipToolOutputs:
    def test_no_snip_below_threshold(self) -> None:
        from obscura.core.compaction import snip_tool_outputs

        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "text": "short"}
                ],
            },
        ]
        result, count, freed = snip_tool_outputs(msgs, threshold_tokens=10_000)
        assert count == 0
        assert freed == 0

    def test_snip_large_output(self) -> None:
        from obscura.core.compaction import snip_tool_outputs

        big_text = "word " * 20_000  # ~27K tokens
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "text": big_text}
                ],
            },
        ]
        result, count, freed = snip_tool_outputs(msgs, threshold_tokens=1_000)
        assert count == 1
        assert freed > 0
        content = result[0]["content"][0]["text"]
        assert "[snipped:" in content

    def test_preserves_non_tool_messages(self) -> None:
        from obscura.core.compaction import snip_tool_outputs

        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        result, count, freed = snip_tool_outputs(msgs)
        assert count == 0
        assert len(result) == 2

    def test_snip_single_output(self) -> None:
        from obscura.core.compaction import snip_single_output

        big = "word " * 20_000
        result = snip_single_output(big, threshold_tokens=1_000)
        assert len(result) < len(big)
        assert "[snipped:" in result

    def test_snip_single_output_small_passthrough(self) -> None:
        from obscura.core.compaction import snip_single_output

        small = "short text"
        assert snip_single_output(small) == small


# ---------------------------------------------------------------------------
# Microcompact tests
# ---------------------------------------------------------------------------


class TestMicrocompact:
    def _backend(self, summary="Boundary summary."):
        b = MagicMock()
        b.complete = AsyncMock(return_value=summary)
        return b

    def test_empty(self) -> None:
        from obscura.core.compaction import microcompact

        r, did, freed = asyncio.get_event_loop().run_until_complete(
            microcompact([], "claude-opus-4-5", self._backend())
        )
        assert r == []
        assert not did

    def test_small_no_compact(self) -> None:
        from obscura.core.compaction import microcompact

        msgs = [{"role": "user", "content": f"msg {i}"} for i in range(4)]
        r, did, freed = asyncio.get_event_loop().run_until_complete(
            microcompact(msgs, "claude-opus-4-5", self._backend(), preserve_recent=3)
        )
        assert not did

    def test_inserts_boundaries(self) -> None:
        from obscura.core.compaction import microcompact

        msgs = []
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append(
                {"role": role, "content": f"Message {i} with some words to pad"}
            )

        r, did, freed = asyncio.get_event_loop().run_until_complete(
            microcompact(msgs, "claude-opus-4-5", self._backend(), preserve_recent=4)
        )
        assert did
        assert len(r) < len(msgs)
        boundary_found = any(
            "[CONTEXT BOUNDARY" in str(m.get("content", "")) for m in r
        )
        assert boundary_found


class TestSplitAtBoundaries:
    def test_small_single_segment(self) -> None:
        from obscura.core.compaction import _split_at_boundaries

        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
        ]
        assert len(_split_at_boundaries(msgs)) == 1

    def test_splits_at_user_after_assistant(self) -> None:
        from obscura.core.compaction import _split_at_boundaries

        msgs = [
            {"role": "user", "content": "first topic"},
            {"role": "assistant", "content": "response 1"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "text": "data"}
                ],
            },
            {"role": "assistant", "content": "response 2"},
            {"role": "user", "content": "new topic"},
            {"role": "assistant", "content": "response 3"},
        ]
        segs = _split_at_boundaries(msgs, min_segment_size=4)
        assert len(segs) >= 1


# ---------------------------------------------------------------------------
# Evaluate compact tier tests
# ---------------------------------------------------------------------------


class TestEvaluateCompactTier:
    def test_ok_on_small(self) -> None:
        from obscura.core.compaction import evaluate_compact_tier

        assert (
            evaluate_compact_tier(
                [{"role": "user", "content": "hi"}], "claude-opus-4-5"
            )
            == "ok"
        )

    def test_snip_tier(self) -> None:
        from obscura.core.compaction import evaluate_compact_tier

        with patch(
            "obscura.core.compaction.estimate_messages_tokens",
            return_value=125_000,
        ):
            assert evaluate_compact_tier([{}], "claude-opus-4-5") == "snip"

    def test_critical_tier(self) -> None:
        from obscura.core.compaction import evaluate_compact_tier

        with patch(
            "obscura.core.compaction.estimate_messages_tokens",
            return_value=185_000,
        ):
            assert evaluate_compact_tier([{}], "claude-opus-4-5") == "critical"


# ---------------------------------------------------------------------------
# Tiered compact integration tests
# ---------------------------------------------------------------------------


class TestTieredCompact:
    def _backend(self, summary="Summary."):
        b = MagicMock()
        b.complete = AsyncMock(return_value=summary)
        return b

    def test_ok_returns_none_strategy(self) -> None:
        from obscura.core.compaction import tiered_compact

        msgs = [{"role": "user", "content": "hi"}]
        r, strategy, freed = asyncio.get_event_loop().run_until_complete(
            tiered_compact(msgs, "claude-opus-4-5", self._backend())
        )
        assert strategy == "none"
        assert freed == 0

    def test_snip_strategy_applied(self) -> None:
        from obscura.core.compaction import tiered_compact

        big_text = "word " * 20_000  # ~27K tokens
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "text": big_text}
                ],
            },
        ]
        # Set snip_at just above the post-snip size so snipping drops us to "ok"
        with patch("obscura.core.compaction.get_compact_thresholds") as mock_t:
            from obscura.core.context_window import CompactThresholds

            mock_t.return_value = CompactThresholds(
                snip_at=12_000,
                compact_at=500_000,
                critical_at=900_000,
                preserve_recent=6,
                context_window=1_000_000,
            )
            r, strategy, freed = asyncio.get_event_loop().run_until_complete(
                tiered_compact(msgs, "claude-opus-4-5", self._backend())
            )
            assert strategy == "snip"
            assert freed > 0
