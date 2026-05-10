"""Tests for context window optimizations in compaction (Change 5).

Change 5: extract_memories max_tokens 1024 -> 512
"""
from __future__ import annotations

import pytest
from typing import Any
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Change 5: memory extraction max_tokens 512
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_memories_calls_complete_with_max_tokens_512() -> None:
    """extract_memories must call backend.complete with max_tokens <= 512."""
    from obscura.core.compaction import extract_memories

    captured: dict[str, Any] = {}

    async def fake_complete(prompt: str, max_tokens: int = 0) -> str:
        captured["max_tokens"] = max_tokens
        return "[]"

    backend = MagicMock()
    backend.complete = fake_complete

    messages = [
        {"role": "user", "content": "What is the auth approach?"},
        {"role": "assistant", "content": "We use JWT with 24h expiry."},
    ]
    await extract_memories(messages, "gpt-4o", backend)

    assert "max_tokens" in captured, "backend.complete was not called"
    assert captured["max_tokens"] <= 512, (
        f"max_tokens was {captured['max_tokens']}, expected <= 512"
    )


@pytest.mark.asyncio
async def test_extract_memories_calls_generate_with_max_tokens_512() -> None:
    """extract_memories must also cap max_tokens via backend.generate."""
    from obscura.core.compaction import extract_memories

    captured: dict[str, Any] = {}

    async def fake_generate(prompt: str, max_tokens: int = 0) -> str:
        captured["max_tokens"] = max_tokens
        return "[]"

    backend = MagicMock(spec=[])  # no .complete, only .generate
    backend.generate = fake_generate

    messages = [
        {"role": "user", "content": "Describe the DB schema"},
        {"role": "assistant", "content": "Postgres with three tables."},
    ]
    await extract_memories(messages, "gpt-4o", backend)

    assert "max_tokens" in captured, "backend.generate was not called"
    assert captured["max_tokens"] <= 512
