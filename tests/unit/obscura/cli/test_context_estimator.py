from __future__ import annotations

from unittest.mock import Mock

from obscura.cli.commands import (
    REPLContext,
    estimate_effective_context_breakdown,
)
from obscura.core.types import ToolSpec


def _ctx(*, backend: str = "codex", tools: list[ToolSpec] | None = None) -> REPLContext:
    client = Mock()
    client.list_tools.return_value = tools or []
    return REPLContext(
        client=client,
        store=Mock(),
        session_id="s1",
        backend=backend,
        model="gpt-5",
        system_prompt="System prompt",
        max_turns=8,
        tools_enabled=True,
        message_history=[("user", "hello"), ("assistant", "world")],
    )


def _tool(name: str = "demo_tool") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Demo tool description",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        handler=lambda **_: "ok",
    )


def test_breakdown_includes_tools_and_pending() -> None:
    ctx = _ctx(tools=[_tool()])
    d = estimate_effective_context_breakdown(ctx, pending_user_text="next prompt")

    assert d["tool_schema_tokens"] > 0
    assert d["pending_tokens"] > 0
    assert d["response_reserve_tokens"] == 4096
    assert d["total_tokens"] == (
        d["system_tokens"]
        + d["history_tokens"]
        + d["pending_tokens"]
        + d["tool_schema_tokens"]
        + d["claude_tool_listing_tokens"]
        + d["response_reserve_tokens"]
    )


def test_breakdown_adds_claude_tool_listing_tokens() -> None:
    codex_ctx = _ctx(backend="codex", tools=[_tool()])
    claude_ctx = _ctx(backend="claude", tools=[_tool()])

    codex_d = estimate_effective_context_breakdown(codex_ctx)
    claude_d = estimate_effective_context_breakdown(claude_ctx)

    assert codex_d["claude_tool_listing_tokens"] == 0
    assert claude_d["claude_tool_listing_tokens"] > 0
    assert claude_d["total_tokens"] > codex_d["total_tokens"]

