"""Tests for EventToIteratorBridge — focused on the cumulative-delta bug.

Background: Copilot's `assistant.message_delta` events have been observed
to ship cumulative `content` instead of incremental `delta_content` on
certain message shapes. The bridge previously fell back to using
`content` as if it were a delta, causing the assistant text to print
N copies of the manifesto opening (one per token-since-cumulative-began).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from obscura.core.enums.agent import ChunkKind
from obscura.core.stream import EventToIteratorBridge


def _delta_event(*, delta_content: str = "", content: str = "") -> SimpleNamespace:
    """Mimic Copilot's `assistant.message_delta` event shape."""
    data = SimpleNamespace(delta_content=delta_content, content=content)
    return SimpleNamespace(data=data)


async def _drain(bridge: EventToIteratorBridge) -> list[Any]:
    chunks: list[Any] = []
    async for chunk in bridge:
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Layer 1: source-side hardening
# ---------------------------------------------------------------------------


class TestSourceHardening:
    def test_proper_incremental_deltas_emit_as_is(self) -> None:
        bridge = EventToIteratorBridge()
        bridge.on_text_delta(_delta_event(delta_content="# A"))
        bridge.on_text_delta(_delta_event(delta_content="bcdef"))
        bridge.finish()
        chunks = asyncio.run(_drain(bridge))
        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert [c.text for c in text_chunks] == ["# A", "bcdef"]

    def test_cumulative_only_event_is_dropped_with_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Event with `content` but no `delta_content` must NOT be treated
        as a delta — that was the bug. We log instead."""
        bridge = EventToIteratorBridge()
        with caplog.at_level(logging.WARNING):
            bridge.on_text_delta(_delta_event(content="cumulative full message"))
        bridge.finish()
        chunks = asyncio.run(_drain(bridge))
        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert text_chunks == []
        assert any(
            "missing delta_content" in rec.message for rec in caplog.records
        )

    def test_bare_string_event_still_works(self) -> None:
        """Codex et al. emit bare-string events as a degenerate path."""
        bridge = EventToIteratorBridge()
        bridge.on_text_delta("hello")
        bridge.finish()
        chunks = asyncio.run(_drain(bridge))
        text_chunks = [c for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert [c.text for c in text_chunks] == ["hello"]


# ---------------------------------------------------------------------------
# Layer 2: defense-in-depth prefix-extension detection
# ---------------------------------------------------------------------------


class TestPrefixExtensionGuard:
    def test_cumulative_delta_through_proper_field_is_normalized(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Even if a provider misuses `delta_content` to send cumulative
        snapshots, the bridge slices the overlap and emits only the new
        tail. This is the symptom we observed in Copilot."""
        bridge = EventToIteratorBridge()
        with caplog.at_level(logging.WARNING):
            bridge.on_text_delta(_delta_event(delta_content="# A"))
            bridge.on_text_delta(_delta_event(delta_content="# A bc"))
            bridge.on_text_delta(_delta_event(delta_content="# A bc def"))
        bridge.finish()
        chunks = asyncio.run(_drain(bridge))
        texts = [c.text for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        # Reassembled, must be the final cumulative content — no duplication.
        assert "".join(texts) == "# A bc def"
        # And the warning fired on the second cumulative delta.
        assert any(
            "cumulative snapshot" in rec.message for rec in caplog.records
        )

    def test_independent_chunks_are_not_falsely_collapsed(self) -> None:
        """If the next delta does NOT start with the prior cumulative text,
        it's a genuine new chunk and must pass through unchanged."""
        bridge = EventToIteratorBridge()
        bridge.on_text_delta(_delta_event(delta_content="hello "))
        bridge.on_text_delta(_delta_event(delta_content="world"))
        bridge.finish()
        chunks = asyncio.run(_drain(bridge))
        texts = [c.text for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert texts == ["hello ", "world"]

    def test_finish_resets_state_for_next_turn(self) -> None:
        """The bridge can be reused across turns — emitted-state must
        reset on finish so a new turn starting with the same prefix as
        the previous turn doesn't get incorrectly trimmed."""
        bridge = EventToIteratorBridge()
        bridge.on_text_delta(_delta_event(delta_content="alpha"))
        bridge.finish()
        _ = asyncio.run(_drain(bridge))

        # Start a fresh stream; same prefix, must come through.
        bridge2 = EventToIteratorBridge()  # In practice each stream gets a new bridge
        bridge2.on_text_delta(_delta_event(delta_content="alpha"))
        bridge2.finish()
        chunks = asyncio.run(_drain(bridge2))
        texts = [c.text for c in chunks if c.kind == ChunkKind.TEXT_DELTA]
        assert texts == ["alpha"]
