"""Reusable test utilities for Obscura.

Provides a fluent :class:`MockBackendBuilder` for constructing mock
backends in < 10 lines, pre-built chunk helpers, fake message classes,
and common tool fixtures.

Usage::

    from obscura.testing import MockBackendBuilder, text_chunks, tool_call_chunks

    backend = (
        MockBackendBuilder()
        .with_turn(text_chunks("Hello world"))
        .with_turn(tool_call_chunks("search", {"q": "weather"}) + text_chunks("72°F"))
        .build()
    )
"""

from obscura.testing.mock_backend import (
    MockBackend,
    MockBackendBuilder,
)
from obscura.testing.chunks import (
    done_chunk,
    error_chunk,
    text_chunk,
    text_chunks,
    thinking_chunk,
    thinking_chunks,
    tool_call_chunks,
    tool_end_chunk,
    tool_start_chunk,
    tool_delta_chunk,
)
from obscura.testing.tools import (
    echo_handler,
    async_echo_handler,
    failing_handler,
    noop_handler,
    make_tool,
    make_registry,
)
from obscura.testing.fakes import (
    FakeTextBlock,
    FakeThinkingBlock,
    FakeToolUseBlock,
    FakeAssistantMessage,
    FakeResultMessage,
    FakeSystemMessage,
    async_iter,
)
from obscura.testing.agents import (
    StubAgent,
    make_stub_agent,
)

__all__ = [
    # Mock backend
    "MockBackend",
    "MockBackendBuilder",
    # Chunk factories
    "done_chunk",
    "error_chunk",
    "text_chunk",
    "text_chunks",
    "thinking_chunk",
    "thinking_chunks",
    "tool_call_chunks",
    "tool_end_chunk",
    "tool_start_chunk",
    "tool_delta_chunk",
    # Tool helpers
    "echo_handler",
    "async_echo_handler",
    "failing_handler",
    "noop_handler",
    "make_tool",
    "make_registry",
    # Fakes (Claude SDK shims)
    "FakeTextBlock",
    "FakeThinkingBlock",
    "FakeToolUseBlock",
    "FakeAssistantMessage",
    "FakeResultMessage",
    "FakeSystemMessage",
    "async_iter",
    # Agent helpers
    "StubAgent",
    "make_stub_agent",
]
