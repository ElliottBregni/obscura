"""Tests for tool record/replay middleware and AgentLoopScenarioExecutor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from obscura.core.hooks import HookRegistry
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
from obscura.parity.models import (
    ScenarioSpec,
    ScenarioStep,
    ScenarioStepKind,
)
from obscura.parity.runner import AgentLoopScenarioExecutor
from obscura.parity.tool_middleware import (
    ToolFixture,
    ToolRecordReplayMiddleware,
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
        )
    )
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


class MockBackend:
    def __init__(self, turn_responses: list[list[StreamChunk]]) -> None:
        self._turns = list(turn_responses)
        self._call_count = 0
        self._registry = ToolRegistry()

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        if self._call_count < len(self._turns):
            chunks = self._turns[self._call_count]
        else:
            chunks = [StreamChunk(kind=ChunkKind.DONE)]
        self._call_count += 1
        for chunk in chunks:
            yield chunk

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        return Message(role=Role.ASSISTANT, content=[], raw=None)

    async def create_session(self, **kwargs: Any) -> SessionRef:
        return SessionRef(session_id="s", backend=Backend.COPILOT)

    async def resume_session(self, ref: SessionRef) -> None:
        return None

    async def list_sessions(self) -> list[SessionRef]:
        return []

    async def delete_session(self, ref: SessionRef) -> None:
        return None

    def register_tool(self, spec: ToolSpec) -> None:
        self._registry.register(spec)

    def register_hook(self, hook: HookPoint, callback: Any) -> None:
        return None

    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    @property
    def native(self) -> NativeHandle:
        return NativeHandle()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


def _echo_handler(msg: str = "") -> str:
    return f"echo:{msg}"


# ---------------------------------------------------------------------------
# ToolRecordReplayMiddleware tests
# ---------------------------------------------------------------------------


class TestRecordMode:
    @pytest.mark.asyncio
    async def test_record_captures_tool_results(self) -> None:
        """Record mode saves tool results as fixtures."""
        hooks = HookRegistry()
        middleware = ToolRecordReplayMiddleware(mode="record")
        middleware.install(hooks)

        # Simulate a TOOL_RESULT after-hook firing
        event = AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="echo",
            tool_input={"msg": "hello"},
            tool_result="echo:hello",
            turn=1,
        )
        await hooks.run_after(event)

        assert len(middleware.recorded) == 1
        assert middleware.recorded[0].tool_name == "echo"
        assert middleware.recorded[0].tool_result == "echo:hello"

    @pytest.mark.asyncio
    async def test_record_multiple_calls(self) -> None:
        """Record mode captures multiple tool results in order."""
        hooks = HookRegistry()
        middleware = ToolRecordReplayMiddleware(mode="record")
        middleware.install(hooks)

        for i in range(3):
            event = AgentEvent(
                kind=AgentEventKind.TOOL_RESULT,
                tool_name=f"tool_{i}",
                tool_result=f"result_{i}",
                turn=1,
            )
            await hooks.run_after(event)

        assert len(middleware.recorded) == 3
        assert [f.tool_name for f in middleware.recorded] == [
            "tool_0", "tool_1", "tool_2"
        ]

    def test_flush_writes_fixtures(self, tmp_path: Path) -> None:
        """Flush writes recorded fixtures to disk as JSON."""
        middleware = ToolRecordReplayMiddleware(
            mode="record",
            fixtures_dir=str(tmp_path / "fixtures"),
        )
        middleware._recorded = [
            ToolFixture(
                tool_name="echo",
                tool_input={"msg": "test"},
                tool_result="echo:test",
            ),
        ]
        middleware.flush()

        fixture_file = tmp_path / "fixtures" / "tool_fixtures.json"
        assert fixture_file.exists()
        data = json.loads(fixture_file.read_text())
        assert len(data) == 1
        assert data[0]["tool_name"] == "echo"


class TestReplayMode:
    @pytest.mark.asyncio
    async def test_replay_returns_fixture(self, tmp_path: Path) -> None:
        """Replay mode returns cached fixture for tool calls."""
        # Write fixture file
        fixtures_dir = tmp_path / "fixtures"
        fixtures_dir.mkdir()
        fixture_data = [
            {
                "tool_name": "echo",
                "tool_input": {"msg": "cached"},
                "tool_result": "cached:result",
                "is_error": False,
            }
        ]
        (fixtures_dir / "tool_fixtures.json").write_text(json.dumps(fixture_data))

        hooks = HookRegistry()
        middleware = ToolRecordReplayMiddleware(
            mode="replay",
            fixtures_dir=str(fixtures_dir),
        )
        middleware.install(hooks)

        # Simulate a TOOL_CALL before-hook
        event = AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="echo",
            tool_input={"msg": "original"},
            turn=1,
        )
        result = await hooks.run_before(event)

        assert result is not None
        assert result.kind == AgentEventKind.TOOL_RESULT
        assert result.tool_result == "cached:result"

    @pytest.mark.asyncio
    async def test_replay_ordering(self, tmp_path: Path) -> None:
        """Replay returns fixtures in recorded order."""
        fixtures_dir = tmp_path / "fixtures"
        fixtures_dir.mkdir()
        fixture_data = [
            {"tool_name": "a", "tool_input": {}, "tool_result": "first", "is_error": False},
            {"tool_name": "b", "tool_input": {}, "tool_result": "second", "is_error": False},
        ]
        (fixtures_dir / "tool_fixtures.json").write_text(json.dumps(fixture_data))

        hooks = HookRegistry()
        middleware = ToolRecordReplayMiddleware(
            mode="replay",
            fixtures_dir=str(fixtures_dir),
        )
        middleware.install(hooks)

        r1 = await hooks.run_before(
            AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="a", turn=1)
        )
        r2 = await hooks.run_before(
            AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="b", turn=1)
        )

        assert r1 is not None and r1.tool_result == "first"
        assert r2 is not None and r2.tool_result == "second"


class TestLiveMode:
    @pytest.mark.asyncio
    async def test_live_no_interception(self) -> None:
        """Live mode doesn't register any hooks."""
        hooks = HookRegistry()
        middleware = ToolRecordReplayMiddleware(mode="live")
        middleware.install(hooks)

        event = AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="echo",
            turn=1,
        )
        result = await hooks.run_before(event)
        # Should pass through unchanged
        assert result is not None
        assert result.kind == AgentEventKind.TOOL_CALL


# ---------------------------------------------------------------------------
# AgentLoopScenarioExecutor tests
# ---------------------------------------------------------------------------


class TestAgentLoopScenarioExecutor:
    @pytest.mark.asyncio
    async def test_basic_execution(self) -> None:
        """Executor runs a text-only scenario successfully."""
        backend = MockBackend([_make_text_chunks("hello world")])
        registry = _make_registry()

        executor = AgentLoopScenarioExecutor(backend, registry)
        spec = ScenarioSpec(
            id="basic",
            title="say hello",
            feature_ids=("text",),
            backend=Backend.COPILOT,
            steps=(
                ScenarioStep(kind=ScenarioStepKind.USER_PROMPT, text="say hello"),
            ),
        )

        result = await executor.execute_async(spec)
        assert result.passed is True
        assert result.scenario_id == "basic"
        assert "text_delta" in result.observed_events

    @pytest.mark.asyncio
    async def test_assert_event_pass(self) -> None:
        """Assert event step passes when event is present."""
        backend = MockBackend([_make_text_chunks("hi")])
        executor = AgentLoopScenarioExecutor(backend, _make_registry())
        spec = ScenarioSpec(
            id="check-event",
            title="check events",
            feature_ids=(),
            backend=Backend.COPILOT,
            steps=(
                ScenarioStep(kind=ScenarioStepKind.USER_PROMPT, text="hi"),
                ScenarioStep(
                    kind=ScenarioStepKind.ASSERT_EVENT,
                    expected_event="agent_done",
                ),
            ),
        )

        result = await executor.execute_async(spec)
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_assert_event_fail(self) -> None:
        """Assert event step fails when event is missing."""
        backend = MockBackend([_make_text_chunks("hi")])
        executor = AgentLoopScenarioExecutor(backend, _make_registry())
        spec = ScenarioSpec(
            id="check-missing",
            title="check missing event",
            feature_ids=(),
            backend=Backend.COPILOT,
            steps=(
                ScenarioStep(kind=ScenarioStepKind.USER_PROMPT, text="hi"),
                ScenarioStep(
                    kind=ScenarioStepKind.ASSERT_EVENT,
                    expected_event="tool_call",  # no tool calls in this run
                ),
            ),
        )

        result = await executor.execute_async(spec)
        assert result.passed is False
        assert "tool_call" in result.details

    @pytest.mark.asyncio
    async def test_record_mode_integration(self, tmp_path: Path) -> None:
        """Executor with record mode captures tool fixtures."""
        spec_tool = ToolSpec(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            handler=_echo_handler,
        )
        backend = MockBackend([
            _make_tool_call_chunks("echo", {"msg": "test"}),
            _make_text_chunks("done"),
        ])
        executor = AgentLoopScenarioExecutor(
            backend, _make_registry(spec_tool), max_turns=5
        )
        fixtures_dir = str(tmp_path / "fixtures")

        spec = ScenarioSpec(
            id="record-test",
            title="record tool calls",
            feature_ids=(),
            backend=Backend.COPILOT,
            tool_mode="record",
            fixtures_dir=fixtures_dir,
            steps=(
                ScenarioStep(kind=ScenarioStepKind.USER_PROMPT, text="echo test"),
            ),
        )

        result = await executor.execute_async(spec)
        assert result.passed is True

        # Verify fixtures were written
        fixture_file = Path(fixtures_dir) / "tool_fixtures.json"
        assert fixture_file.exists()
        data = json.loads(fixture_file.read_text())
        assert len(data) >= 1
        assert data[0]["tool_name"] == "echo"
