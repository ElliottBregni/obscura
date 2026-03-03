"""Tests for run observer and drift detection."""

from __future__ import annotations

import pytest

from obscura.core.supervisor.observability import RunMetrics, RunObserver
from obscura.core.supervisor.types import SupervisorEvent, SupervisorEventKind


class TestRunObserver:
    def test_record_context(self) -> None:
        obs = RunObserver(run_id="r1", session_id="s1")
        obs.record_context(
            prompt_hash="abc123",
            tool_snapshot_hash="def456",
            tool_count=5,
        )
        assert obs.metrics.prompt_hash == "abc123"
        assert obs.metrics.tool_snapshot_hash == "def456"
        assert obs.metrics.tool_count == 5

    def test_observe_events(self) -> None:
        obs = RunObserver(run_id="r1", session_id="s1")
        obs.observe(SupervisorEvent(kind=SupervisorEventKind.MODEL_TURN_START))
        obs.observe(SupervisorEvent(kind=SupervisorEventKind.MODEL_TURN_START))
        obs.observe(SupervisorEvent(kind=SupervisorEventKind.HEARTBEAT))
        obs.observe(SupervisorEvent(kind=SupervisorEventKind.MEMORY_COMMIT))
        obs.observe(SupervisorEvent(kind=SupervisorEventKind.MEMORY_DEDUPLICATED))
        obs.observe(SupervisorEvent(kind=SupervisorEventKind.MEMORY_GATED))
        obs.observe(SupervisorEvent(kind=SupervisorEventKind.HOOK_FIRED))

        m = obs.metrics
        assert m.turn_count == 2
        assert m.heartbeat_count == 1
        assert m.memory_committed == 1
        assert m.memory_deduplicated == 1
        assert m.memory_gated == 1
        assert m.hook_fires == 1

    def test_prompt_drift_detected(self) -> None:
        obs = RunObserver(run_id="r1", session_id="s1")
        obs.record_context(prompt_hash="expected")
        assert obs.check_prompt_drift("expected") is False
        assert obs.check_prompt_drift("different") is True
        assert obs.metrics.drift_detected is True

    def test_tool_drift_detected(self) -> None:
        obs = RunObserver(run_id="r1", session_id="s1")
        obs.record_context(tool_snapshot_hash="expected")
        assert obs.check_tool_drift("expected") is False
        assert obs.check_tool_drift("different") is True
        assert obs.metrics.drift_detected is True

    def test_no_drift_without_context(self) -> None:
        obs = RunObserver(run_id="r1", session_id="s1")
        assert obs.check_prompt_drift("anything") is False
        assert obs.check_tool_drift("anything") is False

    def test_finalize(self) -> None:
        obs = RunObserver(run_id="r1", session_id="s1")
        obs.start()
        obs.observe(SupervisorEvent(kind=SupervisorEventKind.MODEL_TURN_START))
        metrics = obs.finalize()
        assert metrics.duration_ms > 0
        assert metrics.turn_count == 1

    def test_lock_timing(self) -> None:
        obs = RunObserver(run_id="r1", session_id="s1")
        obs.record_lock_acquired(150.0)
        assert obs.metrics.lock_wait_ms == 150.0

    def test_to_dict(self) -> None:
        obs = RunObserver(run_id="r1", session_id="s1")
        obs.record_context(prompt_hash="abc123", tool_count=3)
        obs.start()
        metrics = obs.finalize()
        d = metrics.to_dict()
        assert d["run_id"] == "r1"
        assert d["tool_count"] == 3
        assert "prompt_hash" in d
