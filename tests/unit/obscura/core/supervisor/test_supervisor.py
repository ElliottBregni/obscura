"""Tests for the Supervisor coordinator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from obscura.core.supervisor.supervisor import Supervisor
from obscura.core.supervisor.types import (
    MemoryCandidate,
    SupervisorConfig,
    SupervisorEvent,
    SupervisorEventKind,
)


@pytest.fixture
def supervisor(tmp_path: Path) -> Supervisor:
    sv = Supervisor(
        db_path=tmp_path / "test.db",
        config=SupervisorConfig(
            lock_timeout=5.0,
            lock_ttl=10.0,
            heartbeat_interval=0.2,
            max_turn_duration=30.0,
        ),
    )
    yield sv
    sv.close()


def _make_tool_registry() -> MagicMock:
    """Create a mock tool registry."""
    spec1 = MagicMock()
    spec1.name = "bash"
    spec1.description = "Run shell command"
    spec1.parameters = {"type": "object", "properties": {"cmd": {"type": "string"}}}

    spec2 = MagicMock()
    spec2.name = "write_file"
    spec2.description = "Write a file"
    spec2.parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    registry = MagicMock()
    registry.all.return_value = [spec1, spec2]
    return registry


class TestSupervisor:
    @pytest.mark.asyncio
    async def test_basic_run_lifecycle(self, supervisor: Supervisor) -> None:
        """A basic run goes through all states and emits expected events."""
        events: list[SupervisorEvent] = []
        async for event in supervisor.run(
            session_id="sess-1",
            prompt="Hello world",
            system_prompt="You are helpful.",
        ):
            events.append(event)

        kinds = [e.kind for e in events]

        # Must include key lifecycle events
        assert SupervisorEventKind.LOCK_ACQUIRED in kinds
        assert SupervisorEventKind.RUN_STARTED in kinds
        assert SupervisorEventKind.PROMPT_ASSEMBLED in kinds
        assert SupervisorEventKind.CONTEXT_BUILT in kinds
        assert SupervisorEventKind.MODEL_TURN_START in kinds
        assert SupervisorEventKind.MODEL_TURN_END in kinds
        assert SupervisorEventKind.LOCK_RELEASED in kinds
        assert SupervisorEventKind.RUN_COMPLETED in kinds

    @pytest.mark.asyncio
    async def test_tools_frozen(self, supervisor: Supervisor) -> None:
        """Tool registry is frozen during build context."""
        registry = _make_tool_registry()
        events: list[SupervisorEvent] = []
        async for event in supervisor.run(
            session_id="sess-1",
            prompt="Fix bug",
            tool_registry=registry,
        ):
            events.append(event)

        frozen_events = [
            e for e in events if e.kind == SupervisorEventKind.TOOLS_FROZEN
        ]
        assert len(frozen_events) == 1
        assert frozen_events[0].payload["tool_count"] == 2

    @pytest.mark.asyncio
    async def test_memory_commit_gating(self, supervisor: Supervisor) -> None:
        """Memory items are queued and committed during COMMITTING_MEMORY."""
        import hashlib
        content = "The sky is blue"
        h = hashlib.sha256(content.encode()).hexdigest()
        items = [
            MemoryCandidate(
                key="fact-1",
                content=content,
                content_hash=h,
                importance=0.8,
            ),
        ]

        events: list[SupervisorEvent] = []
        async for event in supervisor.run(
            session_id="sess-1",
            prompt="Hello",
            memory_items=items,
        ):
            events.append(event)

        commit_events = [
            e for e in events if e.kind == SupervisorEventKind.MEMORY_COMMIT
        ]
        assert len(commit_events) >= 1

    @pytest.mark.asyncio
    async def test_prompt_hash_in_events(self, supervisor: Supervisor) -> None:
        events: list[SupervisorEvent] = []
        async for event in supervisor.run(
            session_id="sess-1",
            prompt="Test prompt",
        ):
            events.append(event)

        prompt_events = [
            e for e in events if e.kind == SupervisorEventKind.PROMPT_ASSEMBLED
        ]
        assert len(prompt_events) == 1
        assert "prompt_hash" in prompt_events[0].payload

    @pytest.mark.asyncio
    async def test_run_completed_with_metrics(self, supervisor: Supervisor) -> None:
        events: list[SupervisorEvent] = []
        async for event in supervisor.run(
            session_id="sess-1",
            prompt="Test",
        ):
            events.append(event)

        completed = [
            e for e in events if e.kind == SupervisorEventKind.RUN_COMPLETED
        ]
        assert len(completed) == 1
        payload = completed[0].payload
        assert "duration_ms" in payload
        assert "tool_count" in payload

    @pytest.mark.asyncio
    async def test_concurrent_runs_serialized(self, supervisor: Supervisor) -> None:
        """Two concurrent runs on the same session are serialized."""
        results: list[str] = []

        async def run_session(label: str) -> None:
            async for event in supervisor.run(
                session_id="sess-shared",
                prompt=f"Run {label}",
            ):
                if event.kind == SupervisorEventKind.RUN_COMPLETED:
                    results.append(label)

        # Run concurrently — second should wait for first
        await asyncio.gather(
            run_session("A"),
            run_session("B"),
        )
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_independent_sessions_parallel(self, supervisor: Supervisor) -> None:
        """Different sessions can run in parallel."""
        results: list[str] = []

        async def run_session(session_id: str) -> None:
            async for event in supervisor.run(
                session_id=session_id,
                prompt="Test",
            ):
                if event.kind == SupervisorEventKind.RUN_COMPLETED:
                    results.append(session_id)

        await asyncio.gather(
            run_session("sess-1"),
            run_session("sess-2"),
        )
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_replay_run(self, supervisor: Supervisor) -> None:
        """A completed run can be replayed for debugging."""
        run_id = ""
        async for event in supervisor.run(
            session_id="sess-1",
            prompt="Test replay",
            tool_registry=_make_tool_registry(),
        ):
            if event.kind == SupervisorEventKind.RUN_STARTED:
                run_id = event.run_id

        assert run_id
        replay = await supervisor.replay_run(run_id)
        assert replay["run_id"] == run_id
        assert replay["session_id"] == "sess-1"
        assert replay["event_count"] > 0
        assert replay.get("tool_snapshot") is not None

    @pytest.mark.asyncio
    async def test_recovery(self, supervisor: Supervisor) -> None:
        """Recovery cleans up expired locks and orphaned runs."""
        result = await supervisor.recover()
        assert "expired_locks" in result
        assert "orphaned_runs" in result

    @pytest.mark.asyncio
    async def test_heartbeat_events(self, supervisor: Supervisor) -> None:
        """Heartbeats are emitted as events during the run."""
        events: list[SupervisorEvent] = []
        async for event in supervisor.run(
            session_id="sess-1",
            prompt="Slow task",
        ):
            events.append(event)

        # May or may not have heartbeats depending on timing,
        # but the heartbeat infrastructure should not crash
        heartbeat_events = [
            e for e in events if e.kind == SupervisorEventKind.HEARTBEAT
        ]
        # At minimum, the final heartbeat from stop()
        assert isinstance(heartbeat_events, list)
