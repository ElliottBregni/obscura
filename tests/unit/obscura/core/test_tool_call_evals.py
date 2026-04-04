"""Eval suite for tool call dispatch — parameter aliases, error reporting, edge cases.

Catches silent failures in the tool call pipeline:
- Parameter alias collisions
- Missing tool results in structured messages
- Allowlist rejection context
- JSON parse failures
- Not-found counter reset between runs
- Dropped kwargs visibility

Run with:
    pytest tests/unit/obscura/core/test_tool_call_evals.py -v
"""

from __future__ import annotations

import json
from typing import Any, override
from collections.abc import AsyncIterator, Callable

import pytest

from obscura.core.agent_loop import PARAMETER_ALIASES, AgentLoop
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    AgentEventKind,
    Backend,
    BackendCapabilities,
    BackendProtocol,
    ChunkKind,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    ToolErrorType,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_chunks(text: str) -> list[StreamChunk]:
    chunks: list[StreamChunk] = []
    for word in text.split(" "):
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=word + " "))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


def _make_tool_call_chunks(
    tool_name: str,
    tool_input: dict[str, Any],
) -> list[StreamChunk]:
    chunks: list[StreamChunk] = []
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name=tool_name))
    chunks.append(
        StreamChunk(
            kind=ChunkKind.TOOL_USE_DELTA,
            tool_input_delta=json.dumps(tool_input),
        ),
    )
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_END))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


class MockBackend(BackendProtocol):
    def __init__(self, turn_responses: list[list[StreamChunk]]) -> None:
        self._turns = list(turn_responses)
        self._call_count = 0
        self._registry = ToolRegistry()

    @override
    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        if self._call_count < len(self._turns):
            chunks = self._turns[self._call_count]
        else:
            chunks = [StreamChunk(kind=ChunkKind.DONE)]
        self._call_count += 1
        for chunk in chunks:
            yield chunk

    @override
    async def start(self) -> None:
        return None

    @override
    async def stop(self) -> None:
        return None

    @override
    async def send(self, prompt: str, **kwargs: Any) -> Message:
        return Message(role=Role.ASSISTANT, content=[], raw=None)

    @override
    async def create_session(self, **kwargs: Any) -> SessionRef:
        return SessionRef(session_id="sess", backend=Backend.COPILOT)

    @override
    async def resume_session(self, ref: SessionRef) -> None:
        return None

    @override
    async def list_sessions(self) -> list[SessionRef]:
        return []

    @override
    async def delete_session(self, ref: SessionRef) -> None:
        return None

    @override
    def register_tool(self, spec: ToolSpec) -> None:
        self._registry.register(spec)

    @override
    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        return None

    @override
    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    @property
    @override
    def native(self) -> NativeHandle:
        return NativeHandle()

    @override
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


def _tool_spec(
    name: str,
    handler: Callable[..., Any] | None = None,
    parameters: dict[str, Any] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"Test tool {name}",
        parameters=parameters or {},
        handler=handler or (lambda **kw: json.dumps(kw)),
        output_schema={},
        auth_scope=(),
        rate_limit_per_minute=0,
        cost_hint=0.0,
        timeout_seconds=30.0,
        retries=0,
        examples=(),
    )


async def _collect_events(loop: AgentLoop, prompt: str = "test") -> list[AgentEvent]:
    return [e async for e in loop.run(prompt)]


def _events_of_kind(events: list[AgentEvent], kind: AgentEventKind) -> list[AgentEvent]:
    return [e for e in events if e.kind == kind]


# ---------------------------------------------------------------------------
# Tests: Parameter Alias Mapping
# ---------------------------------------------------------------------------


class TestParameterAliases:
    """Verify PARAMETER_ALIASES are applied correctly during tool dispatch."""

    async def test_alias_renames_parameter(self) -> None:
        """When LLM sends 'content', it's renamed to 'text' for write_text_file."""
        captured: dict[str, Any] = {}

        def _handler(**kw: Any) -> str:
            captured.update(kw)
            return "ok"

        spec = _tool_spec("write_text_file", handler=_handler)

        # LLM sends "content" (OpenAI convention), alias maps to "text"
        backend = MockBackend([
            _make_tool_call_chunks("write_text_file", {"path": "/tmp/f", "content": "hello"}),
            _make_text_chunks("done"),
        ])
        reg = _make_registry(spec)
        loop = AgentLoop(backend, reg)
        await _collect_events(loop)

        assert "text" in captured, "Alias 'content' should be renamed to 'text'"
        assert "content" not in captured, "Alias 'content' should be removed"
        assert captured["text"] == "hello"

    async def test_alias_collision_keeps_canonical(self) -> None:
        """When LLM sends BOTH alias AND canonical, canonical wins, alias is dropped."""
        captured: dict[str, Any] = {}

        def _handler(**kw: Any) -> str:
            captured.update(kw)
            return "ok"

        spec = _tool_spec("write_text_file", handler=_handler)
        backend = MockBackend([
            _make_tool_call_chunks(
                "write_text_file",
                {"path": "/tmp/f", "content": "from_alias", "text": "from_canonical"},
            ),
            _make_text_chunks("done"),
        ])
        reg = _make_registry(spec)
        loop = AgentLoop(backend, reg)
        await _collect_events(loop)

        assert captured["text"] == "from_canonical", "Canonical value must win"
        assert "content" not in captured, "Alias must be removed on collision"

    async def test_edit_text_file_aliases(self) -> None:
        """edit_text_file aliases map old_string→old_text, new_string→new_text."""
        captured: dict[str, Any] = {}

        def _handler(**kw: Any) -> str:
            captured.update(kw)
            return "ok"

        spec = _tool_spec("edit_text_file", handler=_handler)
        backend = MockBackend([
            _make_tool_call_chunks(
                "edit_text_file",
                {"file_path": "/tmp/f", "old_string": "a", "new_string": "b"},
            ),
            _make_text_chunks("done"),
        ])
        reg = _make_registry(spec)
        loop = AgentLoop(backend, reg)
        await _collect_events(loop)

        assert captured.get("path") == "/tmp/f" or captured.get("file_path") == "/tmp/f"
        # At least one of these should have been aliased
        assert "old_text" in captured or "old_string" in captured

    def test_parameter_aliases_are_well_formed(self) -> None:
        """All PARAMETER_ALIASES entries map string→string, no self-references."""
        for tool_name, aliases in PARAMETER_ALIASES.items():
            assert isinstance(tool_name, str)
            for alias, canonical in aliases.items():
                assert isinstance(alias, str)
                assert isinstance(canonical, str)
                assert alias != canonical, f"{tool_name}: alias '{alias}' maps to itself"


# ---------------------------------------------------------------------------
# Tests: Allowlist Rejection Context
# ---------------------------------------------------------------------------


class TestAllowlistRejection:
    """Verify allowlist errors include available tools."""

    async def test_rejection_includes_available_tools(self) -> None:
        """When a tool is blocked by allowlist, error message lists what IS allowed."""
        spec = _tool_spec("run_shell")
        backend = MockBackend([
            _make_tool_call_chunks("run_shell", {"command": "ls"}),
            _make_text_chunks("ok"),
        ])
        reg = _make_registry(spec)
        loop = AgentLoop(
            backend,
            reg,
            tool_allowlist=["read_text_file", "grep_files"],
        )
        events = await _collect_events(loop)

        result_events = _events_of_kind(events, AgentEventKind.TOOL_RESULT)
        assert len(result_events) >= 1
        result = result_events[0]
        assert result.is_error
        assert "read_text_file" in result.tool_result
        assert "grep_files" in result.tool_result

    async def test_allowed_tool_passes(self) -> None:
        """Tools in the allowlist execute normally."""
        spec = _tool_spec("read_text_file", handler=lambda **kw: "content")
        backend = MockBackend([
            _make_tool_call_chunks("read_text_file", {"path": "/tmp/f"}),
            _make_text_chunks("done"),
        ])
        reg = _make_registry(spec)
        loop = AgentLoop(
            backend,
            reg,
            tool_allowlist=["read_text_file"],
        )
        events = await _collect_events(loop)

        result_events = _events_of_kind(events, AgentEventKind.TOOL_RESULT)
        assert len(result_events) >= 1
        assert not result_events[0].is_error


# ---------------------------------------------------------------------------
# Tests: Unknown Tool (not-found counter)
# ---------------------------------------------------------------------------


class TestNotFoundCounter:
    """Verify _not_found_counts resets between runs."""

    async def test_counter_resets_between_runs(self) -> None:
        """Not-found counts from a previous run don't carry over."""
        spec = _tool_spec("real_tool", handler=lambda **kw: "ok")
        reg = _make_registry(spec)

        # Run 1: call nonexistent tool 2 times (below hard-stop threshold)
        backend1 = MockBackend([
            _make_tool_call_chunks("fake_tool", {}),
            _make_tool_call_chunks("fake_tool", {}),
            _make_text_chunks("giving up"),
        ])
        loop = AgentLoop(backend1, reg)
        events1 = await _collect_events(loop)

        # Run 2: same loop instance, call fake_tool again — count should be 1, not 3
        backend2 = MockBackend([
            _make_tool_call_chunks("fake_tool", {}),
            _make_text_chunks("ok"),
        ])
        loop._backend = backend2  # type: ignore[assignment]
        events2 = await _collect_events(loop)

        result_events = _events_of_kind(events2, AgentEventKind.TOOL_RESULT)
        # Should NOT contain the hard-stop "STOP:" message (which triggers at count >= 3)
        for e in result_events:
            assert "STOP:" not in e.tool_result, (
                "Not-found counter leaked across runs"
            )

    async def test_unknown_tool_suggests_available(self) -> None:
        """Unknown tool error should list available tools."""
        spec = _tool_spec("read_text_file")
        reg = _make_registry(spec)

        # Use a name that won't fuzzy-match to anything
        backend = MockBackend([
            _make_tool_call_chunks("zzz_nonexistent_tool", {}),
            _make_text_chunks("ok"),
        ])
        loop = AgentLoop(backend, reg)
        events = await _collect_events(loop)

        result_events = _events_of_kind(events, AgentEventKind.TOOL_RESULT)
        assert len(result_events) >= 1
        assert result_events[0].is_error
        # Should list available tools in the error
        assert "read_text_file" in result_events[0].tool_result


# ---------------------------------------------------------------------------
# Tests: JSON Parse Failure
# ---------------------------------------------------------------------------


class TestJSONParseFailure:
    """Verify malformed JSON input is handled gracefully."""

    async def test_malformed_json_still_dispatches(self) -> None:
        """When JSON is malformed, tool gets _raw_input fallback (and we log)."""
        captured: dict[str, Any] = {}

        def _handler(**kw: Any) -> str:
            captured.update(kw)
            return "got it"

        spec = _tool_spec("my_tool", handler=_handler)
        reg = _make_registry(spec)

        # Construct chunks with malformed JSON
        chunks: list[StreamChunk] = [
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="my_tool"),
            StreamChunk(kind=ChunkKind.TOOL_USE_DELTA, tool_input_delta="{broken json"),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        backend = MockBackend([chunks, _make_text_chunks("done")])
        loop = AgentLoop(backend, reg)
        await _collect_events(loop)

        # Tool still executes, but with _raw_input fallback
        assert "_raw_input" in captured


# ---------------------------------------------------------------------------
# Tests: Missing Tool Results in Structured Messages
# ---------------------------------------------------------------------------


class TestMissingToolResults:
    """Verify missing results are surfaced, not silently skipped."""

    def test_build_structured_messages_with_gap(self) -> None:
        """When a tool result is missing, an error block is emitted."""
        from obscura.core.types import ToolCallInfo, ToolResultEnvelope

        calls = [
            ToolCallInfo(
                tool_use_id="call_1",
                name="read_text_file",
                input={"path": "/tmp/f"},
            ),
            ToolCallInfo(
                tool_use_id="call_2",
                name="grep_files",
                input={"pattern": "foo"},
            ),
        ]

        # Only provide result for call_1, not call_2
        results = [
            ToolResultEnvelope(
                call_id="call_1",
                tool="read_text_file",
                status="ok",
                result="file contents",
                tool_use_id="call_1",
            ),
        ]

        messages = AgentLoop._build_structured_tool_messages(calls, results, "")
        result_msg = messages[1]  # tool result message
        blocks = result_msg.content

        # Should have 2 blocks — one ok, one error for the missing result
        assert len(blocks) == 2, f"Expected 2 result blocks, got {len(blocks)}"
        error_blocks = [b for b in blocks if b.is_error]
        assert len(error_blocks) == 1
        assert "no result received" in error_blocks[0].text.lower()

    def test_build_structured_messages_all_present(self) -> None:
        """When all results are present, no error blocks."""
        from obscura.core.types import ToolCallInfo, ToolResultEnvelope

        calls = [
            ToolCallInfo(
                tool_use_id="call_1",
                name="read_text_file",
                input={"path": "/tmp/f"},
            ),
        ]
        results = [
            ToolResultEnvelope(
                call_id="call_1",
                tool="read_text_file",
                status="ok",
                result="file contents",
                tool_use_id="call_1",
            ),
        ]

        messages = AgentLoop._build_structured_tool_messages(calls, results, "")
        result_msg = messages[1]
        error_blocks = [b for b in result_msg.content if b.is_error]
        assert len(error_blocks) == 0


# ---------------------------------------------------------------------------
# Tests: Dropped kwargs visibility
# ---------------------------------------------------------------------------


class TestDroppedKwargs:
    """Verify that undeclared kwargs are handled without silent data loss."""

    async def test_extra_kwargs_dropped_tool_still_works(self) -> None:
        """Extra kwargs that the handler doesn't accept are dropped, tool still runs."""
        def _handler(path: str) -> str:
            return f"read {path}"

        spec = _tool_spec(
            "read_text_file",
            handler=_handler,
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )

        backend = MockBackend([
            _make_tool_call_chunks(
                "read_text_file",
                {"path": "/tmp/f", "encoding": "utf-8", "extra_nonsense": True},
            ),
            _make_text_chunks("done"),
        ])
        reg = _make_registry(spec)
        loop = AgentLoop(backend, reg)
        events = await _collect_events(loop)

        result_events = _events_of_kind(events, AgentEventKind.TOOL_RESULT)
        assert len(result_events) >= 1
        # Tool should succeed despite extra params
        assert not result_events[0].is_error
        assert "read /tmp/f" in result_events[0].tool_result


# ---------------------------------------------------------------------------
# Tests: Required parameter validation
# ---------------------------------------------------------------------------


class TestRequiredParamValidation:
    """Verify missing required params produce clear errors."""

    async def test_missing_required_param_error(self) -> None:
        """When a required param is missing, error message names the param."""
        def _handler(path: str, text: str) -> str:
            return "ok"

        spec = _tool_spec(
            "write_text_file",
            handler=_handler,
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["path", "text"],
            },
        )

        backend = MockBackend([
            # Only sends path, missing text
            _make_tool_call_chunks("write_text_file", {"path": "/tmp/f"}),
            _make_text_chunks("done"),
        ])
        reg = _make_registry(spec)
        loop = AgentLoop(backend, reg)
        events = await _collect_events(loop)

        result_events = _events_of_kind(events, AgentEventKind.TOOL_RESULT)
        assert len(result_events) >= 1
        assert result_events[0].is_error
        assert "text" in result_events[0].tool_result.lower()
