"""Tests for per-source / per-tool adapter dispatch."""

from __future__ import annotations

from obscura.cli.renderer.adapters import (
    MCPToolEventAdapter,
    RuntimeEventAdapter,
    ShellToolEventAdapter,
)
from obscura.cli.renderer.normalizer import SignalNormalizer
from obscura.cli.renderer.ui_event import DisplayMode, UiEventKind, UiSeverity
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent


def _evt(kind: AgentEventKind, **kwargs) -> AgentEvent:
    return AgentEvent(kind=kind, **kwargs)


# ── MCP adapter ──────────────────────────────────────────────────────────


def test_mcp_adapter_handles_mcp_prefixed_tools() -> None:
    a = MCPToolEventAdapter()
    assert a.handles(_evt(AgentEventKind.TOOL_CALL, tool_name="mcp__github__list_repos"))
    assert not a.handles(_evt(AgentEventKind.TOOL_CALL, tool_name="run_command"))
    assert not a.handles(_evt(AgentEventKind.TEXT_DELTA, text="hi"))


def test_mcp_adapter_extracts_server_and_tool() -> None:
    a = MCPToolEventAdapter()
    out = list(
        a.adapt(
            _evt(
                AgentEventKind.TOOL_CALL,
                tool_name="mcp__github__list_repos",
                tool_input={"org": "anthropic"},
                tool_use_id="tu_1",
            ),
        ),
    )
    assert len(out) == 1
    ui = out[0]
    assert ui.kind == UiEventKind.TOOL_CALL
    assert ui.title == "list_repos"
    assert ui.provider == "mcp:github"
    assert ui.metadata["mcp_server"] == "github"
    assert ui.metadata["mcp_tool"] == "list_repos"
    assert ui.metadata["transport"] == "mcp"


def test_mcp_adapter_handles_malformed_name() -> None:
    a = MCPToolEventAdapter()
    out = list(
        a.adapt(
            _evt(
                AgentEventKind.TOOL_CALL,
                tool_name="mcp__just_server",  # missing tool segment
                tool_use_id="tu_2",
            ),
        ),
    )
    # No crash; metadata records empty tool slot.
    assert len(out) == 1
    assert out[0].metadata["mcp_server"] == "just_server"
    assert out[0].metadata["mcp_tool"] == ""


# ── Shell adapter ────────────────────────────────────────────────────────


def test_shell_adapter_recognises_run_command() -> None:
    a = ShellToolEventAdapter()
    assert a.handles(_evt(AgentEventKind.TOOL_CALL, tool_name="run_command"))
    assert a.handles(_evt(AgentEventKind.TOOL_RESULT, tool_name="bash"))
    assert not a.handles(_evt(AgentEventKind.TOOL_CALL, tool_name="read_file"))


def test_shell_adapter_titles_call_with_command() -> None:
    a = ShellToolEventAdapter()
    out = list(
        a.adapt(
            _evt(
                AgentEventKind.TOOL_CALL,
                tool_name="run_command",
                tool_input={"command": "ls -la"},
                tool_use_id="tu_3",
            ),
        ),
    )
    assert out[0].title == "$ ls -la"
    assert out[0].provider == "shell"


def test_shell_adapter_marks_nonzero_exit_as_error() -> None:
    a = ShellToolEventAdapter()
    payload = '{"ok": false, "exit_code": 1, "stdout": "", "stderr": "command not found"}'
    out = list(
        a.adapt(
            _evt(
                AgentEventKind.TOOL_RESULT,
                tool_name="run_command",
                tool_result=payload,
                tool_use_id="tu_4",
            ),
        ),
    )
    assert out[0].severity == UiSeverity.ERROR
    assert out[0].metadata["exit_code"] == 1
    # Body falls back to stderr when stdout is empty + exit != 0.
    assert "command not found" in (out[0].content or "")


def test_shell_adapter_zero_exit_stays_info() -> None:
    a = ShellToolEventAdapter()
    payload = '{"ok": true, "exit_code": 0, "stdout": "hello\\n", "stderr": ""}'
    out = list(
        a.adapt(
            _evt(
                AgentEventKind.TOOL_RESULT,
                tool_name="run_command",
                tool_result=payload,
                tool_use_id="tu_5",
            ),
        ),
    )
    assert out[0].severity == UiSeverity.INFO
    assert out[0].metadata["exit_code"] == 0


def test_shell_adapter_resilient_to_non_json_payload() -> None:
    a = ShellToolEventAdapter()
    out = list(
        a.adapt(
            _evt(
                AgentEventKind.TOOL_RESULT,
                tool_name="run_command",
                tool_result="raw text not json",
                tool_use_id="tu_6",
            ),
        ),
    )
    # Doesn't crash; severity stays INFO since exit_code unknown.
    assert len(out) == 1
    assert out[0].severity == UiSeverity.INFO


# ── Adapter chain ordering ───────────────────────────────────────────────


def test_normalizer_routes_mcp_tools_to_mcp_adapter() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    out = norm.normalize(
        _evt(
            AgentEventKind.TOOL_CALL,
            tool_name="mcp__github__list_repos",
            tool_input={"org": "anthropic"},
            tool_use_id="tu_7",
        ),
    )
    assert out[0].provider == "mcp:github"


def test_normalizer_routes_shell_tools_to_shell_adapter() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    out = norm.normalize(
        _evt(
            AgentEventKind.TOOL_CALL,
            tool_name="run_command",
            tool_input={"command": "echo hi"},
            tool_use_id="tu_8",
        ),
    )
    assert out[0].title == "$ echo hi"


def test_normalizer_falls_through_to_runtime_adapter() -> None:
    norm = SignalNormalizer(mode=DisplayMode.NORMAL)
    out = norm.normalize(
        _evt(
            AgentEventKind.TOOL_CALL,
            tool_name="read_file",
            tool_input={"path": "x.py"},
            tool_use_id="tu_9",
        ),
    )
    # Default adapter doesn't set provider for native tools.
    assert out[0].provider is None
    assert out[0].title == "read_file"


# ── Sanity: RuntimeEventAdapter handles every kind ──────────────────────


def test_runtime_adapter_always_handles() -> None:
    a = RuntimeEventAdapter()
    for kind in AgentEventKind:
        assert a.handles(_evt(kind))
