"""Tests for post-tool-call hallucination suppression in the agent loop.

When a model generates text AFTER a tool_use block in the same turn, it is
hallucinating the tool outcome before seeing the real result (e.g. "permission
denied", "file written successfully", "tool needs approval").  The agent loop
should suppress these TEXT_DELTA chunks so they are neither displayed to the
user nor fed back into the conversation history.

These tests cover every tool type and various hallucination patterns to ensure
the suppression is robust.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable, override

import pytest

from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    AgentEventKind,
    Backend,
    BackendCapabilities,
    ChunkKind,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)
from obscura.core.agent_loop import AgentLoop
from obscura.core.types import BackendProtocol


# ---------------------------------------------------------------------------
# Mock infrastructure
# ---------------------------------------------------------------------------


class MockBackend(BackendProtocol):
    """A mock backend that returns pre-configured stream chunks per turn."""

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


def _noop_handler(**kwargs: Any) -> str:
    return json.dumps({"ok": True})


def _tool_spec(name: str, desc: str = "") -> ToolSpec:
    """Create a minimal ToolSpec with a no-op handler."""
    return ToolSpec(
        name=name,
        description=desc or f"Test tool: {name}",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_noop_handler,
    )


def _make_tool_chunks_with_hallucination(
    tool_name: str,
    tool_input: dict[str, Any],
    hallucinated_text: str,
    *,
    preceding_text: str = "",
    use_tool_use_end: bool = False,
) -> list[StreamChunk]:
    """Create chunks simulating: [optional preamble] → tool_use → hallucinated text → DONE.

    This is the pattern that triggers suppression: the model generates text
    AFTER the tool_use block, speculating about the outcome.
    """
    chunks: list[StreamChunk] = []
    if preceding_text:
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=preceding_text))
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name=tool_name))
    chunks.append(
        StreamChunk(
            kind=ChunkKind.TOOL_USE_DELTA,
            tool_input_delta=json.dumps(tool_input),
        )
    )
    if use_tool_use_end:
        chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_END))
    # Hallucinated text AFTER the tool call
    for word in hallucinated_text.split(" "):
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=word + " "))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


def _make_clean_tool_chunks(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    preceding_text: str = "",
) -> list[StreamChunk]:
    """Create a clean tool call with no post-tool text."""
    chunks: list[StreamChunk] = []
    if preceding_text:
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=preceding_text))
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name=tool_name))
    chunks.append(
        StreamChunk(
            kind=ChunkKind.TOOL_USE_DELTA,
            tool_input_delta=json.dumps(tool_input),
        )
    )
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


def _make_text_chunks(text: str) -> list[StreamChunk]:
    """Create text-only response chunks."""
    chunks: list[StreamChunk] = []
    for word in text.split(" "):
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=word + " "))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


def _collect_text(events: list[AgentEvent]) -> str:
    """Extract all TEXT_DELTA content from events."""
    return "".join(e.text for e in events if e.kind == AgentEventKind.TEXT_DELTA)


def _collect_tool_calls(events: list[AgentEvent]) -> list[AgentEvent]:
    return [e for e in events if e.kind == AgentEventKind.TOOL_CALL]


def _collect_tool_results(events: list[AgentEvent]) -> list[AgentEvent]:
    return [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]


# ---------------------------------------------------------------------------
# Test suite: hallucination suppression per tool type
# ---------------------------------------------------------------------------


class TestHallucinationSuppression:
    """Verify that TEXT_DELTA chunks after a tool_use block are suppressed."""

    # -- write_text_file ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_write_text_file_permission_denied_hallucination(self) -> None:
        """Model hallucinates 'permission denied' after write_text_file call."""
        spec = _tool_spec("write_text_file")
        turn1 = _make_tool_chunks_with_hallucination(
            "write_text_file",
            {"path": "/tmp/test.txt", "text": "hello"},
            "The tool call was denied — I don't currently have permission to write files.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("write a file")]
        text = _collect_text(events)

        assert "permission" not in text.lower()
        assert "denied" not in text.lower()
        assert _collect_tool_calls(events), "tool call should still be emitted"
        assert _collect_tool_results(events), "tool result should still be emitted"

    @pytest.mark.asyncio
    async def test_write_text_file_success_hallucination(self) -> None:
        """Model hallucinates success message after write_text_file call."""
        spec = _tool_spec("write_text_file")
        turn1 = _make_tool_chunks_with_hallucination(
            "write_text_file",
            {"path": "/tmp/test.txt", "text": "hello"},
            "The file has been written successfully to /tmp/test.txt.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("write a file")]
        text = _collect_text(events)

        assert "written successfully" not in text.lower()

    # -- read_text_file -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_read_text_file_hallucination(self) -> None:
        """Model hallucinates file contents after read_text_file call."""
        spec = _tool_spec("read_text_file")
        turn1 = _make_tool_chunks_with_hallucination(
            "read_text_file",
            {"path": "/etc/hosts"},
            "The file contains: 127.0.0.1 localhost. Here's the full content...",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("read /etc/hosts")]
        text = _collect_text(events)

        assert "127.0.0.1" not in text
        assert "full content" not in text.lower()

    # -- run_shell ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_run_shell_output_hallucination(self) -> None:
        """Model hallucinates shell output after run_shell call."""
        spec = _tool_spec("run_shell")
        turn1 = _make_tool_chunks_with_hallucination(
            "run_shell",
            {"command": "ls -la"},
            "drwxr-xr-x 5 user staff 160 Mar 1 12:00 . The directory listing shows...",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("list files")]
        text = _collect_text(events)

        assert "drwxr-xr-x" not in text
        assert "directory listing" not in text.lower()

    @pytest.mark.asyncio
    async def test_run_shell_error_hallucination(self) -> None:
        """Model hallucinates command error after run_shell call."""
        spec = _tool_spec("run_shell")
        turn1 = _make_tool_chunks_with_hallucination(
            "run_shell",
            {"command": "rm -rf /"},
            "Error: Permission denied. The command failed because you don't have root access.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("delete everything")]
        text = _collect_text(events)

        assert "permission denied" not in text.lower()
        assert "command failed" not in text.lower()

    # -- run_python3 --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_run_python_output_hallucination(self) -> None:
        """Model hallucinates Python execution output."""
        spec = _tool_spec("run_python3")
        turn1 = _make_tool_chunks_with_hallucination(
            "run_python3",
            {"code": "print('hello')"},
            "Output: hello\nThe code executed successfully and printed 'hello' to stdout.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("run python")]
        text = _collect_text(events)

        assert "executed successfully" not in text.lower()
        assert "Output: hello" not in text

    # -- web_search ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_web_search_results_hallucination(self) -> None:
        """Model hallucinates search results after web_search call."""
        spec = _tool_spec("web_search")
        turn1 = _make_tool_chunks_with_hallucination(
            "web_search",
            {"query": "python asyncio"},
            "Here are the top results: 1. Python asyncio documentation 2. Real Python tutorial...",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("search for asyncio")]
        text = _collect_text(events)

        assert "top results" not in text.lower()
        assert "Real Python" not in text

    # -- web_fetch ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_web_fetch_content_hallucination(self) -> None:
        """Model hallucinates page content after web_fetch call."""
        spec = _tool_spec("web_fetch")
        turn1 = _make_tool_chunks_with_hallucination(
            "web_fetch",
            {"url": "https://example.com"},
            "The page contains: Welcome to Example.com. This domain is for use in examples.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("fetch example.com")]
        text = _collect_text(events)

        assert "Welcome to Example" not in text
        assert "domain is for use" not in text.lower()

    # -- list_directory -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_directory_hallucination(self) -> None:
        """Model hallucinates directory listing after list_directory call."""
        spec = _tool_spec("list_directory")
        turn1 = _make_tool_chunks_with_hallucination(
            "list_directory",
            {"path": "/tmp"},
            "The directory contains: file1.txt, file2.py, data/. There are 3 items.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("list /tmp")]
        text = _collect_text(events)

        assert "file1.txt" not in text
        assert "3 items" not in text

    # -- append_text_file ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_append_text_file_hallucination(self) -> None:
        """Model hallucinates success after append_text_file call."""
        spec = _tool_spec("append_text_file")
        turn1 = _make_tool_chunks_with_hallucination(
            "append_text_file",
            {"path": "/tmp/log.txt", "text": "new line"},
            "Text has been appended to /tmp/log.txt successfully.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("append to log")]
        text = _collect_text(events)

        assert "appended" not in text.lower()

    # -- make_directory -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_make_directory_hallucination(self) -> None:
        """Model hallucinates success after make_directory call."""
        spec = _tool_spec("make_directory")
        turn1 = _make_tool_chunks_with_hallucination(
            "make_directory",
            {"path": "/tmp/newdir"},
            "Directory /tmp/newdir has been created successfully.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("mkdir")]
        text = _collect_text(events)

        assert "created successfully" not in text.lower()

    # -- remove_path --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_remove_path_hallucination(self) -> None:
        """Model hallucinates removal confirmation after remove_path call."""
        spec = _tool_spec("remove_path")
        turn1 = _make_tool_chunks_with_hallucination(
            "remove_path",
            {"path": "/tmp/old.txt"},
            "The file /tmp/old.txt has been removed. It no longer exists.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("delete file")]
        text = _collect_text(events)

        assert "removed" not in text.lower()
        assert "no longer exists" not in text.lower()

    # -- task (delegation) --------------------------------------------------

    @pytest.mark.asyncio
    async def test_task_delegation_hallucination(self) -> None:
        """Model hallucinates sub-agent result after task delegation call."""
        spec = _tool_spec("task")
        turn1 = _make_tool_chunks_with_hallucination(
            "task",
            {"prompt": "analyze this code", "type": "explore"},
            "The sub-agent has completed the analysis and found 3 issues.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("delegate analysis")]
        text = _collect_text(events)

        assert "sub-agent" not in text.lower()
        assert "3 issues" not in text

    # -- MCP-prefixed tool names --------------------------------------------

    @pytest.mark.asyncio
    async def test_mcp_prefixed_tool_hallucination(self) -> None:
        """Model uses mcp__server__tool naming and hallucinates outcome."""
        spec = _tool_spec("write_text_file")
        turn1 = _make_tool_chunks_with_hallucination(
            "mcp__obscura_tools__write_text_file",
            {"path": "/tmp/test.txt", "text": "hello"},
            "I've written the file. The operation completed without errors.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("write via mcp")]
        text = _collect_text(events)

        assert "written the file" not in text.lower()
        assert "completed without errors" not in text.lower()

    # -- get_environment ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_environment_hallucination(self) -> None:
        """Model hallucinates env vars after get_environment call."""
        spec = _tool_spec("get_environment")
        turn1 = _make_tool_chunks_with_hallucination(
            "get_environment",
            {},
            "Your environment has HOME=/Users/user, PATH=/usr/bin:/bin...",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("show env")]
        text = _collect_text(events)

        assert "HOME=" not in text
        assert "PATH=" not in text

    # -- get_system_info ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_system_info_hallucination(self) -> None:
        """Model hallucinates system info after get_system_info call."""
        spec = _tool_spec("get_system_info")
        turn1 = _make_tool_chunks_with_hallucination(
            "get_system_info",
            {},
            "System: macOS 15.3, CPU: Apple M1 Pro, Memory: 16GB",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("system info")]
        text = _collect_text(events)

        assert "Apple M1" not in text
        assert "16GB" not in text


# ---------------------------------------------------------------------------
# Test suite: edge cases and complex scenarios
# ---------------------------------------------------------------------------


class TestHallucinationEdgeCases:
    """Edge cases for the suppression logic."""

    @pytest.mark.asyncio
    async def test_preamble_text_before_tool_is_preserved(self) -> None:
        """Text BEFORE a tool call should NOT be suppressed."""
        spec = _tool_spec("write_text_file")
        turn1 = _make_tool_chunks_with_hallucination(
            "write_text_file",
            {"path": "/tmp/test.txt", "text": "hello"},
            "Permission denied.",
            preceding_text="I'll write that file for you.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("write file")]
        text = _collect_text(events)

        # Preamble preserved
        assert "write that file" in text.lower()
        # Post-tool hallucination suppressed
        assert "permission denied" not in text.lower()

    @pytest.mark.asyncio
    async def test_text_only_turn_not_suppressed(self) -> None:
        """When there are no tool calls, all text should pass through normally."""
        turn1 = _make_text_chunks("Here is your answer with full details.")
        backend = MockBackend([turn1])
        loop = AgentLoop(backend, _make_registry())

        events = [e async for e in loop.run("question")]
        text = _collect_text(events)

        assert "answer" in text.lower()
        assert "full details" in text.lower()

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_hallucination_between(self) -> None:
        """Model calls two tools with hallucinated text between them."""
        spec1 = _tool_spec("read_text_file")
        spec2 = _tool_spec("write_text_file")
        chunks: list[StreamChunk] = [
            # First tool call
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="read_text_file"),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_input_delta=json.dumps({"path": "/tmp/input.txt"}),
            ),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            # Hallucinated text between tools
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="File read. "),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Now writing output. "),
            # Second tool call
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="write_text_file"),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_input_delta=json.dumps({"path": "/tmp/output.txt", "text": "data"}),
            ),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            # Hallucinated text after second tool
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Both operations completed. "),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([chunks, turn2])
        loop = AgentLoop(backend, _make_registry(spec1, spec2))

        events = [e async for e in loop.run("copy file")]
        text = _collect_text(events)

        assert "File read" not in text
        assert "Now writing" not in text
        assert "Both operations" not in text
        assert len(_collect_tool_calls(events)) == 2

    @pytest.mark.asyncio
    async def test_hallucination_with_tool_use_end(self) -> None:
        """Suppression works with explicit TOOL_USE_END chunks."""
        spec = _tool_spec("run_shell")
        turn1 = _make_tool_chunks_with_hallucination(
            "run_shell",
            {"command": "echo hello"},
            "Output: hello\n",
            use_tool_use_end=True,
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("echo")]
        text = _collect_text(events)

        assert "Output: hello" not in text

    @pytest.mark.asyncio
    async def test_tool_result_still_emitted_despite_suppression(self) -> None:
        """Even when hallucination is suppressed, actual tool results are emitted."""
        spec = _tool_spec("write_text_file")
        turn1 = _make_tool_chunks_with_hallucination(
            "write_text_file",
            {"path": "/tmp/test.txt", "text": "hello"},
            "Permission denied. You need elevated access.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("write")]
        results = _collect_tool_results(events)

        assert len(results) == 1
        assert results[0].is_error is False
        assert "ok" in results[0].tool_result.lower()

    @pytest.mark.asyncio
    async def test_suppression_resets_per_turn(self) -> None:
        """Suppression flag resets each turn — text in turn 2 is not suppressed."""
        spec = _tool_spec("read_text_file")
        # Turn 1: tool call with hallucination
        turn1 = _make_tool_chunks_with_hallucination(
            "read_text_file",
            {"path": "/tmp/test.txt"},
            "The file does not exist.",
        )
        # Turn 2: pure text response (should NOT be suppressed)
        turn2 = _make_text_chunks("The file contained important data.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("read file")]
        text = _collect_text(events)

        # Turn 1 hallucination suppressed
        assert "does not exist" not in text.lower()
        # Turn 2 text preserved
        assert "important data" in text.lower()

    @pytest.mark.asyncio
    async def test_long_hallucination_fully_suppressed(self) -> None:
        """A multi-sentence hallucination after a tool call is fully suppressed."""
        spec = _tool_spec("run_shell")
        hallucination = (
            "The command executed successfully. "
            "Here is the output: total 48 "
            "drwxr-xr-x 12 user staff 384 Mar 1 12:00 . "
            "The directory has 12 items. "
            "Would you like me to explain any of these files?"
        )
        turn1 = _make_tool_chunks_with_hallucination(
            "run_shell",
            {"command": "ls -la"},
            hallucination,
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("list files")]
        text = _collect_text(events)

        assert "executed successfully" not in text.lower()
        assert "total 48" not in text
        assert "12 items" not in text
        assert "explain any" not in text.lower()

    @pytest.mark.asyncio
    async def test_clean_tool_call_no_text_suppressed(self) -> None:
        """When model generates no post-tool text, nothing is wrongly suppressed."""
        spec = _tool_spec("write_text_file")
        turn1 = _make_clean_tool_chunks(
            "write_text_file",
            {"path": "/tmp/test.txt", "text": "hello"},
        )
        turn2 = _make_text_chunks("The file was written.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("write")]
        text = _collect_text(events)

        # Turn 2 text should be preserved
        assert "file was written" in text.lower()

    @pytest.mark.asyncio
    async def test_thinking_deltas_not_affected(self) -> None:
        """THINKING_DELTA chunks should still pass through even after tool_use."""
        spec = _tool_spec("run_shell")
        chunks: list[StreamChunk] = [
            StreamChunk(kind=ChunkKind.THINKING_DELTA, text="Let me think..."),
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="run_shell"),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_input_delta=json.dumps({"command": "ls"}),
            ),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            # Thinking after tool should still pass
            StreamChunk(kind=ChunkKind.THINKING_DELTA, text="Now processing..."),
            # But text after tool should be suppressed
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Output was good."),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([chunks, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("list")]
        thinking = [e for e in events if e.kind == AgentEventKind.THINKING_DELTA]
        text = _collect_text(events)

        # Both thinking events preserved
        assert len(thinking) == 2
        assert "Let me think" in thinking[0].text
        assert "Now processing" in thinking[1].text
        # Text after tool suppressed
        assert "Output was good" not in text

    @pytest.mark.asyncio
    async def test_hallucination_not_in_structured_messages(self) -> None:
        """Suppressed text should not appear in structured messages sent back to model."""
        spec = _tool_spec("write_text_file")
        hallucinated = "HALLUCINATED: Permission was denied for this operation."
        turn1 = _make_tool_chunks_with_hallucination(
            "write_text_file",
            {"path": "/tmp/test.txt", "text": "hello"},
            hallucinated,
            preceding_text="I'll write the file.",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("write")]

        # Check TURN_COMPLETE event text — it contains accumulated turn_text
        turn_completes = [e for e in events if e.kind == AgentEventKind.TURN_COMPLETE]
        for tc in turn_completes:
            if tc.turn == 1:
                assert "HALLUCINATED" not in (tc.text or "")

    @pytest.mark.asyncio
    async def test_waiting_for_permission_hallucination(self) -> None:
        """Common hallucination: 'waiting for permission' or 'needs approval'."""
        spec = _tool_spec("run_shell")
        turn1 = _make_tool_chunks_with_hallucination(
            "run_shell",
            {"command": "sudo reboot"},
            "This tool needs approval before it can execute. Waiting for permission...",
        )
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("reboot")]
        text = _collect_text(events)

        assert "needs approval" not in text.lower()
        assert "waiting for permission" not in text.lower()

    @pytest.mark.asyncio
    async def test_tool_call_with_preamble_and_hallucination(self) -> None:
        """Preamble before tool call is kept; hallucination after is dropped."""
        spec = _tool_spec("web_search")
        chunks: list[StreamChunk] = [
            # Preamble — should be kept
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Let me "),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="search for that. "),
            # Tool call
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="web_search"),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_input_delta=json.dumps({"query": "test"}),
            ),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            # Hallucination — should be suppressed
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="I found "),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="several results. "),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="The top result is "),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="from Wikipedia. "),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        turn2 = _make_text_chunks("Here are the actual results.")
        backend = MockBackend([chunks, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("search")]
        text = _collect_text(events)

        assert "search for that" in text.lower()
        assert "I found" not in text
        assert "Wikipedia" not in text
        assert "actual results" in text.lower()

    @pytest.mark.asyncio
    async def test_three_sequential_tool_calls_with_hallucinations(self) -> None:
        """Three tool calls in one turn, each followed by hallucinated text."""
        spec1 = _tool_spec("read_text_file")
        spec2 = _tool_spec("run_python3")
        spec3 = _tool_spec("write_text_file")
        chunks: list[StreamChunk] = [
            # Tool 1
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="read_text_file"),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_input_delta=json.dumps({"path": "/tmp/data.csv"}),
            ),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="File read OK. "),
            # Tool 2
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="run_python3"),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_input_delta=json.dumps({"code": "import pandas"}),
            ),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Analysis complete. "),
            # Tool 3
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="write_text_file"),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_input_delta=json.dumps({"path": "/tmp/out.txt", "text": "result"}),
            ),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Results saved. "),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        turn2 = _make_text_chunks("Pipeline finished.")
        backend = MockBackend([chunks, turn2])
        loop = AgentLoop(backend, _make_registry(spec1, spec2, spec3))

        events = [e async for e in loop.run("process data")]
        text = _collect_text(events)

        assert "File read OK" not in text
        assert "Analysis complete" not in text
        assert "Results saved" not in text
        assert len(_collect_tool_calls(events)) == 3
        assert len(_collect_tool_results(events)) == 3
        assert "Pipeline finished" in text

    @pytest.mark.asyncio
    async def test_error_chunks_still_emitted_after_tool(self) -> None:
        """ERROR chunks should still pass through even after a tool_use."""
        spec = _tool_spec("run_shell")
        chunks: list[StreamChunk] = [
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="run_shell"),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_DELTA,
                tool_input_delta=json.dumps({"command": "ls"}),
            ),
            StreamChunk(kind=ChunkKind.TOOL_USE_END),
            # Text after tool — suppressed
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="This should be hidden."),
            # Error chunk — should still be emitted
            StreamChunk(kind=ChunkKind.ERROR, text="stream error"),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        backend = MockBackend([chunks])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("run")]
        text = _collect_text(events)
        errors = [e for e in events if e.kind == AgentEventKind.ERROR]

        assert "hidden" not in text.lower()
        assert len(errors) == 1
        assert "stream error" in errors[0].text

    @pytest.mark.asyncio
    async def test_max_turns_respected_with_suppression(self) -> None:
        """Suppression should not interfere with max_turns limit."""
        spec = _tool_spec("run_shell")
        # Every turn: tool call + hallucination → needs next turn
        turns = []
        for i in range(5):
            turns.append(
                _make_tool_chunks_with_hallucination(
                    "run_shell",
                    {"command": f"step {i}"},
                    f"Step {i} completed successfully.",
                )
            )
        backend = MockBackend(turns)
        loop = AgentLoop(backend, _make_registry(spec), max_turns=3)

        events = [e async for e in loop.run("multi-step")]
        text = _collect_text(events)

        # No hallucinated text
        assert "completed successfully" not in text.lower()
        # Should have at most 3 turns
        turn_starts = [e for e in events if e.kind == AgentEventKind.TURN_START]
        assert len(turn_starts) <= 3
