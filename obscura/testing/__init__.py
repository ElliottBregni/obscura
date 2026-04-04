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

from obscura.testing.agents import (
    StubAgent,
    make_stub_agent,
)
from obscura.testing.chunks import (
    done_chunk,
    error_chunk,
    text_chunk,
    text_chunks,
    thinking_chunk,
    thinking_chunks,
    tool_call_chunks,
    tool_delta_chunk,
    tool_end_chunk,
    tool_start_chunk,
)
from obscura.testing.fakes import (
    FakeAssistantMessage,
    FakeResultMessage,
    FakeSystemMessage,
    FakeTextBlock,
    FakeThinkingBlock,
    FakeToolUseBlock,
    async_iter,
)
from obscura.testing.mock_backend import (
    MockBackend,
    MockBackendBuilder,
)
from obscura.testing.tools import (
    async_echo_handler,
    echo_handler,
    failing_handler,
    make_registry,
    make_tool,
    noop_handler,
)

__all__ = [
    "FakeAssistantMessage",
    "FakeResultMessage",
    "FakeSystemMessage",
    # Fakes (Claude SDK shims)
    "FakeTextBlock",
    "FakeThinkingBlock",
    "FakeToolUseBlock",
    # Mock backend
    "MockBackend",
    "MockBackendBuilder",
    # Agent helpers
    "StubAgent",
    "async_echo_handler",
    "async_iter",
    # Chunk factories
    "done_chunk",
    # Tool helpers
    "echo_handler",
    "error_chunk",
    "failing_handler",
    "make_registry",
    "make_stub_agent",
    "make_tool",
    "noop_handler",
    "text_chunk",
    "text_chunks",
    "thinking_chunk",
    "thinking_chunks",
    "tool_call_chunks",
    "tool_delta_chunk",
    "tool_end_chunk",
    "tool_start_chunk",
]
