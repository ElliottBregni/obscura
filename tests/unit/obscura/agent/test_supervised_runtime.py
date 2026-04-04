"""Tests for SupervisedRuntime — the Supervisor ↔ AgentRuntime glue layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.agent.supervised_runtime import (
    SupervisedRuntime,
    SupervisedRuntimeConfig,
    _AgentLoopAdaptor,
)
from obscura.core.supervisor.types import SupervisorEvent, SupervisorEventKind
from obscura.core.types import AgentEvent, AgentEventKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_event(kind: AgentEventKind, text: str = "") -> AgentEvent:
    return AgentEvent(kind=kind, text=text)


def _make_mock_agent(events: list[AgentEvent] | None = None) -> MagicMock:
    """Return a mock Agent whose stream_loop() yields the given events."""
    agent = MagicMock()
    agent.id = "agent-test-001"
    agent.config = MagicMock()
    agent.config.name = "coordinator"
    agent._broker = None

    async def _stream_loop(prompt: str, *, max_turns: int | None = None, **kw: Any):
        for ev in events or []:
            yield ev

    agent.stream_loop = _stream_loop
    agent.start = AsyncMock()
    agent.stop = AsyncMock()
    return agent


def _make_mock_runtime(agent: MagicMock) -> MagicMock:
    runtime = MagicMock()
    runtime.spawn = MagicMock(return_value=agent)
    return runtime


# ---------------------------------------------------------------------------
# _AgentLoopAdaptor
# ---------------------------------------------------------------------------


class TestAgentLoopAdaptor:
    @pytest.mark.asyncio
    async def test_forwards_events(self) -> None:
        events = [
            _make_agent_event(AgentEventKind.TURN_START),
            _make_agent_event(AgentEventKind.TEXT_DELTA, text="hello"),
            _make_agent_event(AgentEventKind.TURN_COMPLETE),
        ]
        agent = _make_mock_agent(events)
        adaptor = _AgentLoopAdaptor(agent, max_turns=5)

        collected: list[AgentEvent] = []
        async for ev in adaptor.run("do something", session_id="s1"):
            collected.append(ev)

        assert len(collected) == 3
        assert collected[1].text == "hello"

    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        agent = _make_mock_agent([])
        adaptor = _AgentLoopAdaptor(agent)

        collected = [ev async for ev in adaptor.run("prompt")]
        assert collected == []

    @pytest.mark.asyncio
    async def test_session_id_ignored(self) -> None:
        """session_id is accepted for compat but not forwarded."""
        agent = _make_mock_agent([_make_agent_event(AgentEventKind.TURN_COMPLETE)])
        adaptor = _AgentLoopAdaptor(agent)
        collected = [ev async for ev in adaptor.run("p", session_id="irrelevant")]
        assert len(collected) == 1


# ---------------------------------------------------------------------------
# SupervisedRuntimeConfig defaults
# ---------------------------------------------------------------------------


class TestSupervisedRuntimeConfig:
    def test_defaults(self) -> None:
        cfg = SupervisedRuntimeConfig()
        assert cfg.coordinator_name == "coordinator"
        assert cfg.coordinator_model == "copilot"
        assert cfg.can_delegate is True
        assert cfg.fresh_agent_per_run is False
        assert cfg.memory_namespace == "supervised"

    def test_custom(self) -> None:
        cfg = SupervisedRuntimeConfig(
            coordinator_model="claude",
            coordinator_max_iterations=20,
            fresh_agent_per_run=True,
        )
        assert cfg.coordinator_model == "claude"
        assert cfg.coordinator_max_iterations == 20
        assert cfg.fresh_agent_per_run is True


# ---------------------------------------------------------------------------
# SupervisedRuntime construction
# ---------------------------------------------------------------------------


class TestSupervisedRuntimeInit:
    def test_creates_supervisor(self, tmp_path: Path) -> None:
        sr = SupervisedRuntime(db_path=tmp_path / "s.db")
        assert sr.supervisor is not None
        assert sr.runtime is None  # lazy — not created yet
        assert sr.coordinator is None

    def test_accepts_config(self, tmp_path: Path) -> None:
        cfg = SupervisedRuntimeConfig(coordinator_model="claude")
        sr = SupervisedRuntime(db_path=tmp_path / "s.db", config=cfg)
        assert sr._config.coordinator_model == "claude"


# ---------------------------------------------------------------------------
# SupervisedRuntime.run — integration with mocked internals
# ---------------------------------------------------------------------------


class TestSupervisedRuntimeRun:
    @pytest.mark.asyncio
    async def test_run_yields_supervisor_events(self, tmp_path: Path) -> None:
        """SupervisedRuntime.run() should yield SupervisorEvents end-to-end."""
        agent_events = [
            _make_agent_event(AgentEventKind.TURN_START),
            _make_agent_event(AgentEventKind.TEXT_DELTA, text="done"),
            _make_agent_event(AgentEventKind.TURN_COMPLETE),
        ]
        mock_agent = _make_mock_agent(agent_events)
        mock_runtime = _make_mock_runtime(mock_agent)

        sr = SupervisedRuntime(db_path=tmp_path / "s.db")

        with patch.object(sr, "_get_or_create_runtime", return_value=mock_runtime):
            events: list[SupervisorEvent] = []
            async for ev in sr.run(session_id="sess-abc", prompt="do the thing"):
                events.append(ev)

        kinds = {ev.kind for ev in events}
        # Must include at least LOCK_ACQUIRED and RUN_COMPLETED
        assert SupervisorEventKind.LOCK_ACQUIRED in kinds
        assert SupervisorEventKind.RUN_COMPLETED in kinds

    @pytest.mark.asyncio
    async def test_reuses_coordinator_across_runs(self, tmp_path: Path) -> None:
        """With fresh_agent_per_run=False, agent.start() is only called once."""
        agent_events = [_make_agent_event(AgentEventKind.TURN_COMPLETE)]
        mock_agent = _make_mock_agent(agent_events)
        mock_runtime = _make_mock_runtime(mock_agent)

        cfg = SupervisedRuntimeConfig(fresh_agent_per_run=False)
        sr = SupervisedRuntime(db_path=tmp_path / "s.db", config=cfg)

        with patch.object(sr, "_get_or_create_runtime", return_value=mock_runtime):
            async for _ in sr.run("s1", "first run"):
                pass
            async for _ in sr.run("s2", "second run"):
                pass

        # spawn() called once — coordinator reused
        assert mock_runtime.spawn.call_count == 1
        assert mock_agent.start.call_count == 1

    @pytest.mark.asyncio
    async def test_fresh_agent_per_run(self, tmp_path: Path) -> None:
        """With fresh_agent_per_run=True, a new agent is spawned each time."""
        agent_events = [_make_agent_event(AgentEventKind.TURN_COMPLETE)]

        # Two distinct mock agents for two runs
        agent_a = _make_mock_agent(agent_events)
        agent_b = _make_mock_agent(agent_events)
        mock_runtime = MagicMock()
        mock_runtime.spawn = MagicMock(side_effect=[agent_a, agent_b])

        cfg = SupervisedRuntimeConfig(fresh_agent_per_run=True)
        sr = SupervisedRuntime(db_path=tmp_path / "s.db", config=cfg)

        with patch.object(sr, "_get_or_create_runtime", return_value=mock_runtime):
            async for _ in sr.run("s1", "first"):
                pass
            async for _ in sr.run("s2", "second"):
                pass

        assert mock_runtime.spawn.call_count == 2
        agent_a.stop.assert_called_once()
        agent_b.stop.assert_called_once()


# ---------------------------------------------------------------------------
# SupervisedRuntime.close
# ---------------------------------------------------------------------------


class TestSupervisedRuntimeClose:
    @pytest.mark.asyncio
    async def test_close_stops_coordinator(self, tmp_path: Path) -> None:
        mock_agent = _make_mock_agent([])
        sr = SupervisedRuntime(db_path=tmp_path / "s.db")
        sr._coordinator = mock_agent  # inject directly

        await sr.close()

        mock_agent.stop.assert_called_once()
        assert sr.coordinator is None

    @pytest.mark.asyncio
    async def test_close_with_no_coordinator(self, tmp_path: Path) -> None:
        sr = SupervisedRuntime(db_path=tmp_path / "s.db")
        # Should not raise even if coordinator was never spawned
        await sr.close()


# ---------------------------------------------------------------------------
# SupervisedRuntime.recover
# ---------------------------------------------------------------------------


class TestSupervisedRuntimeRecover:
    @pytest.mark.asyncio
    async def test_recover_delegates_to_supervisor(self, tmp_path: Path) -> None:
        sr = SupervisedRuntime(db_path=tmp_path / "s.db")
        result = await sr.recover()
        assert "expired_locks" in result
        assert "orphaned_runs" in result
