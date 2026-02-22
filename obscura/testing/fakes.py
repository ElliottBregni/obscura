"""Fake message classes for Claude SDK stream tests.

Consolidates ``_FakeTextBlock``, ``_FakeThinkingBlock``, etc. that were
defined in ``test_sdk_stream.py``.  Each class's ``__name__`` is patched
to match the real Claude SDK type so ``type(obj).__name__`` checks pass.

Usage::

    from obscura.testing.fakes import FakeAssistantMessage, FakeTextBlock, async_iter
    from obscura.core.stream import ClaudeIteratorAdapter

    msg = FakeAssistantMessage([FakeTextBlock("hello")])
    adapter = ClaudeIteratorAdapter(async_iter([msg]))
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

__all__ = [
    "FakeTextBlock",
    "FakeThinkingBlock",
    "FakeToolUseBlock",
    "FakeAssistantMessage",
    "FakeResultMessage",
    "FakeSystemMessage",
    "async_iter",
]


class FakeTextBlock:
    """Mimics Claude SDK ``TextBlock``."""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeThinkingBlock:
    """Mimics Claude SDK ``ThinkingBlock``."""

    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class FakeToolUseBlock:
    """Mimics Claude SDK ``ToolUseBlock``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.id = "tool-123"
        self.input: dict[str, str] = {"key": "value"}


class FakeAssistantMessage:
    """Mimics Claude SDK ``AssistantMessage``."""

    def __init__(self, content: list[Any]) -> None:
        self.content = content


class FakeResultMessage:
    """Mimics Claude SDK ``ResultMessage``."""

    session_id = "sess-abc"


class FakeSystemMessage:
    """Mimics Claude SDK ``SystemMessage``."""

    subtype = "info"


# Rename to match the real Claude SDK type names (for __name__-based dispatch).
FakeTextBlock.__name__ = "TextBlock"
FakeThinkingBlock.__name__ = "ThinkingBlock"
FakeToolUseBlock.__name__ = "ToolUseBlock"
FakeAssistantMessage.__name__ = "AssistantMessage"
FakeResultMessage.__name__ = "ResultMessage"
FakeSystemMessage.__name__ = "SystemMessage"


async def async_iter(items: list[Any]) -> AsyncIterator[Any]:
    """Create an async iterator from a list.  Useful for feeding fakes into adapters."""
    for item in items:
        yield item
