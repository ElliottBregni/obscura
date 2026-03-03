"""Tests for obscura.core.context_window and obscura.core.compaction."""
from __future__ import annotations
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


class TestEstimateTokens:
    def test_empty(self):
        from obscura.core.context_window import estimate_tokens
        assert estimate_tokens("") == 0

    def test_short_text(self):
        from obscura.core.context_window import estimate_tokens
        assert 1 <= estimate_tokens("hello world") <= 10

    def test_longer_proportional(self):
        from obscura.core.context_window import estimate_tokens
        assert estimate_tokens("hello world foo bar baz qux") > estimate_tokens("hello")

    def test_code_block(self):
        from obscura.core.context_window import estimate_tokens
        assert estimate_tokens("def foo():\n    return 42\n") > 0


class TestGetContextWindow:
    def test_exact_claude(self):
        from obscura.core.context_window import get_context_window
        assert get_context_window("claude-opus-4-5") == 200_000

    def test_exact_openai(self):
        from obscura.core.context_window import get_context_window
        assert get_context_window("gpt-4o") == 128_000

    def test_exact_gemini(self):
        from obscura.core.context_window import get_context_window
        assert get_context_window("gemini-2.0-flash") == 1_000_000

    def test_prefix_match(self):
        from obscura.core.context_window import get_context_window
        assert get_context_window("claude-sonnet-4-5-20241022") == 200_000

    def test_unknown_returns_default(self):
        from obscura.core.context_window import get_context_window
        assert get_context_window("totally-unknown-xyz") == 100_000


class TestEstimateMessagesTokens:
    def test_empty_list(self):
        from obscura.core.context_window import estimate_messages_tokens
        assert estimate_messages_tokens([]) == 0

    def test_overhead_per_message(self):
        from obscura.core.context_window import estimate_messages_tokens
        # empty content = 0 tokens + 4 overhead
        assert estimate_messages_tokens([{"role": "user", "content": ""}]) == 4

    def test_two_messages_more_than_one(self):
        from obscura.core.context_window import estimate_messages_tokens
        one = estimate_messages_tokens([{"role": "user", "content": "hello"}])
        two = estimate_messages_tokens([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}])
        assert two > one

    def test_list_content(self):
        from obscura.core.context_window import estimate_messages_tokens
        msgs = [{"role": "assistant", "content": [{"type": "text", "text": "Let me help."}, {"type": "tool_use", "id": "tu_1", "name": "read", "input": {}}]}]
        assert estimate_messages_tokens(msgs) > 0


class TestEvaluateContextStatus:
    def test_ok_on_small_history(self):
        from obscura.core.context_window import evaluate_context_status
        status = evaluate_context_status([{"role": "user", "content": "hi"}], "claude-opus-4-5")
        assert not status.should_warn
        assert not status.should_block

    def test_warn_threshold(self):
        from obscura.core.context_window import evaluate_context_status, CONTEXT_WINDOW_WARN_BELOW_TOKENS
        with patch("obscura.core.context_window.estimate_messages_tokens", return_value=200_000 - 30_000 - 4096):
            s = evaluate_context_status([], "claude-opus-4-5")
            assert s.should_warn and not s.should_block

    def test_block_threshold(self):
        from obscura.core.context_window import evaluate_context_status
        with patch("obscura.core.context_window.estimate_messages_tokens", return_value=200_000 - 10_000 - 4096):
            s = evaluate_context_status([], "claude-opus-4-5")
            assert s.should_block

    def test_str_repr(self):
        from obscura.core.context_window import evaluate_context_status
        s = evaluate_context_status([{"role": "user", "content": "hi"}], "gpt-4o")
        assert any(x in str(s) for x in ["[OK]", "[WARN]", "[BLOCK]"])


class TestRepairToolPairs:
    def test_all_paired_unchanged(self):
        from obscura.core.compaction import repair_tool_pairs
        msgs = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1", "name": "f", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}]},
        ]
        assert len(repair_tool_pairs(msgs)) == 2

    def test_removes_orphaned(self):
        from obscura.core.compaction import repair_tool_pairs
        msgs = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_gone", "content": "x"}]},
            {"role": "assistant", "content": "Hello"},
        ]
        result = repair_tool_pairs(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"

    def test_partial_repair(self):
        from obscura.core.compaction import repair_tool_pairs
        msgs = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_good", "name": "r", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_good", "content": "data"},
                {"type": "tool_result", "tool_use_id": "tu_gone", "content": "x"},
            ]},
        ]
        result = repair_tool_pairs(msgs)
        assert len(result) == 2
        assert len(result[1]["content"]) == 1

    def test_empty_list(self):
        from obscura.core.compaction import repair_tool_pairs
        assert repair_tool_pairs([]) == []


class TestCompactHistory:
    def _backend(self, summary="Summary."):
        b = MagicMock(); b.complete = AsyncMock(return_value=summary); return b

    def _msgs(self, n, big=False):
        w = "word " * (500 if big else 10)
        return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"Msg {i}: {w}"} for i in range(n)]

    def test_empty(self):
        from obscura.core.compaction import compact_history
        r, d = asyncio.get_event_loop().run_until_complete(compact_history([], "claude-opus-4-5", self._backend()))
        assert r == [] and not d

    def test_small_no_compact(self):
        from obscura.core.compaction import compact_history
        msgs = self._msgs(5)
        _, did = asyncio.get_event_loop().run_until_complete(compact_history(msgs, "claude-opus-4-5", self._backend()))
        assert not did

    def test_compaction_reduces(self):
        from obscura.core.compaction import compact_history
        msgs = self._msgs(50)
        with patch("obscura.core.compaction.get_context_window", return_value=1000):
            result, did = asyncio.get_event_loop().run_until_complete(compact_history(msgs, "tiny", self._backend()))
        assert did and len(result) < len(msgs)

    def test_no_orphaned_tool_results_after_compact(self):
        from obscura.core.compaction import compact_history
        msgs = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_old", "name": "r", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_old", "content": "data"}]},
        ] + self._msgs(30)
        with patch("obscura.core.compaction.get_context_window", return_value=500):
            result, _ = asyncio.get_event_loop().run_until_complete(compact_history(msgs, "tiny", self._backend()))
        tool_use_ids = {b["id"] for m in result for b in (m.get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_use"}
        for m in result:
            for b in (m.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    assert b["tool_use_id"] in tool_use_ids


class TestAdaptiveChunkRatio:
    def test_normal_uses_base(self):
        from obscura.core.compaction import _compute_adaptive_chunk_ratio
        msgs = [{"role": "user", "content": "hi"}]
        assert _compute_adaptive_chunk_ratio(msgs, 200_000) == 0.4

    def test_large_msgs_reduce_ratio(self):
        from obscura.core.compaction import _compute_adaptive_chunk_ratio
        msgs = [{"role": "user", "content": "word " * 3000}]
        ratio = _compute_adaptive_chunk_ratio(msgs, 20_000)
        assert ratio < 0.4

    def test_floor_at_min(self):
        from obscura.core.compaction import _compute_adaptive_chunk_ratio, MIN_CHUNK_RATIO
        msgs = [{"role": "user", "content": "word " * 100_000}]
        assert _compute_adaptive_chunk_ratio(msgs, 10_000) >= MIN_CHUNK_RATIO
