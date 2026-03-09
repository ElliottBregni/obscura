"""Tests for obscura.core.lifecycle_events — LifecycleEvent model."""

from __future__ import annotations

import dataclasses
import time

import pytest

from obscura.core.lifecycle_events import LifecycleEvent


# ---------------------------------------------------------------------------
# Creation and defaults
# ---------------------------------------------------------------------------


class TestLifecycleEvent:
    def test_creation_with_required_fields(self) -> None:
        e = LifecycleEvent(timestamp=1000.0, event_type="agent_start")
        assert e.timestamp == 1000.0
        assert e.event_type == "agent_start"

    def test_defaults(self) -> None:
        e = LifecycleEvent(timestamp=0.0, event_type="test")
        assert e.workspace == ""
        assert e.agent == ""
        assert e.plugin == ""
        assert e.tool == ""
        assert e.status == ""
        assert e.duration_ms == 0
        assert e.metadata == {}

    def test_all_fields(self) -> None:
        e = LifecycleEvent(
            timestamp=1709000000.0,
            event_type="tool_call",
            workspace="code-mode",
            agent="reviewer",
            plugin="gws",
            tool="gws.drive.files.list",
            status="ok",
            duration_ms=42,
            metadata={"request_id": "abc123"},
        )
        assert e.workspace == "code-mode"
        assert e.agent == "reviewer"
        assert e.plugin == "gws"
        assert e.tool == "gws.drive.files.list"
        assert e.status == "ok"
        assert e.duration_ms == 42
        assert e.metadata["request_id"] == "abc123"

    def test_frozen(self) -> None:
        e = LifecycleEvent(timestamp=1.0, event_type="test")
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.event_type = "changed"  # type: ignore[misc]

    def test_metadata_default_empty_dict(self) -> None:
        e = LifecycleEvent(timestamp=0.0, event_type="test")
        assert isinstance(e.metadata, dict)
        assert len(e.metadata) == 0

    def test_metadata_mutable_contents(self) -> None:
        """Dict contents can be mutated even though the dataclass is frozen."""
        e = LifecycleEvent(timestamp=0.0, event_type="test")
        e.metadata["key"] = "value"
        assert e.metadata["key"] == "value"

    @pytest.mark.parametrize("status", ["ok", "error", "denied", "skipped"])
    def test_status_values(self, status: str) -> None:
        e = LifecycleEvent(timestamp=0.0, event_type="test", status=status)
        assert e.status == status


# ---------------------------------------------------------------------------
# Ordering and collections
# ---------------------------------------------------------------------------


class TestLifecycleEventOrdering:
    def test_timestamp_ordering(self) -> None:
        events = [
            LifecycleEvent(timestamp=3.0, event_type="c"),
            LifecycleEvent(timestamp=1.0, event_type="a"),
            LifecycleEvent(timestamp=2.0, event_type="b"),
        ]
        sorted_events = sorted(events, key=lambda e: e.timestamp)
        assert [e.event_type for e in sorted_events] == ["a", "b", "c"]

    def test_multiple_events_different_types(self) -> None:
        events = [
            LifecycleEvent(timestamp=1.0, event_type="agent_start", agent="ag1"),
            LifecycleEvent(timestamp=2.0, event_type="tool_call", tool="run_shell"),
            LifecycleEvent(timestamp=3.0, event_type="agent_stop", agent="ag1"),
        ]
        assert len(events) == 3
        assert events[0].event_type == "agent_start"
        assert events[-1].event_type == "agent_stop"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestLifecycleEventEdgeCases:
    def test_zero_duration(self) -> None:
        e = LifecycleEvent(timestamp=0.0, event_type="test", duration_ms=0)
        assert e.duration_ms == 0

    def test_empty_strings(self) -> None:
        e = LifecycleEvent(
            timestamp=0.0,
            event_type="",
            workspace="",
            agent="",
        )
        assert e.event_type == ""
        assert e.workspace == ""
