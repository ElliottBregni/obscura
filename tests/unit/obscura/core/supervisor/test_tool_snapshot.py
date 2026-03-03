"""Tests for frozen tool registry snapshots."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from obscura.core.supervisor.tool_snapshot import (
    FrozenToolEntry,
    FrozenToolRegistry,
    ToolSnapshotStore,
)


class TestFrozenToolEntry:
    def test_creation(self) -> None:
        entry = FrozenToolEntry(
            name="bash",
            description="Run a shell command",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            order_index=0,
        )
        assert entry.name == "bash"
        assert entry.order_index == 0
        assert entry.schema_hash  # non-empty

    def test_to_dict_roundtrip(self) -> None:
        entry = FrozenToolEntry(
            name="bash",
            description="Run a shell command",
            parameters={"type": "object"},
            order_index=0,
        )
        d = entry.to_dict()
        restored = FrozenToolEntry.from_dict(d)
        assert restored.name == entry.name
        assert restored.parameters == entry.parameters


class TestFrozenToolRegistry:
    def _make_spec(self, name: str, params: dict | None = None) -> MagicMock:
        spec = MagicMock()
        spec.name = name
        spec.description = f"Tool: {name}"
        spec.parameters = params or {"type": "object"}
        return spec

    def test_from_specs_sorted(self) -> None:
        specs = [
            self._make_spec("zebra"),
            self._make_spec("alpha"),
            self._make_spec("middle"),
        ]
        snapshot = FrozenToolRegistry.from_specs(specs)
        assert snapshot.tool_names == ("alpha", "middle", "zebra")

    def test_deterministic_hash(self) -> None:
        specs = [self._make_spec("a"), self._make_spec("b")]
        s1 = FrozenToolRegistry.from_specs(specs)
        s2 = FrozenToolRegistry.from_specs(specs)
        assert s1.hash == s2.hash

    def test_hash_changes_with_different_tools(self) -> None:
        s1 = FrozenToolRegistry.from_specs([self._make_spec("a")])
        s2 = FrozenToolRegistry.from_specs([self._make_spec("b")])
        assert s1.hash != s2.hash

    def test_allowlist_filter(self) -> None:
        specs = [self._make_spec("a"), self._make_spec("b"), self._make_spec("c")]
        snapshot = FrozenToolRegistry.from_specs(specs, allowlist=["a", "c"])
        assert snapshot.tool_names == ("a", "c")

    def test_denylist_filter(self) -> None:
        specs = [self._make_spec("a"), self._make_spec("b"), self._make_spec("c")]
        snapshot = FrozenToolRegistry.from_specs(specs, denylist=["b"])
        assert snapshot.tool_names == ("a", "c")

    def test_json_roundtrip(self) -> None:
        specs = [self._make_spec("a"), self._make_spec("b")]
        original = FrozenToolRegistry.from_specs(specs)
        json_str = original.to_json()
        restored = FrozenToolRegistry.from_json(json_str)
        assert restored.hash == original.hash
        assert restored.tool_names == original.tool_names

    def test_get_tool(self) -> None:
        specs = [self._make_spec("bash")]
        snapshot = FrozenToolRegistry.from_specs(specs)
        tool = snapshot.get("bash")
        assert tool is not None
        assert tool.name == "bash"
        assert snapshot.get("nonexistent") is None

    def test_contains(self) -> None:
        specs = [self._make_spec("bash")]
        snapshot = FrozenToolRegistry.from_specs(specs)
        assert snapshot.contains("bash") is True
        assert snapshot.contains("nope") is False

    def test_empty_registry(self) -> None:
        snapshot = FrozenToolRegistry.from_specs([])
        assert snapshot.tool_count == 0
        assert snapshot.hash  # still produces a hash

    def test_from_registrations_preserves_order(self) -> None:
        regs = [
            {"name": "c_tool", "order_index": 2, "active": True},
            {"name": "a_tool", "order_index": 0, "active": True},
            {"name": "b_tool", "order_index": 1, "active": True},
        ]
        snapshot = FrozenToolRegistry.from_registrations(regs)
        assert snapshot.tool_names == ("a_tool", "b_tool", "c_tool")

    def test_from_registrations_filters_inactive(self) -> None:
        regs = [
            {"name": "active_tool", "order_index": 0, "active": True},
            {"name": "inactive_tool", "order_index": 1, "active": False},
        ]
        snapshot = FrozenToolRegistry.from_registrations(regs)
        assert snapshot.tool_names == ("active_tool",)


class TestToolSnapshotStore:
    def test_save_and_load(self, tmp_path: Path) -> None:
        store = ToolSnapshotStore(tmp_path / "test.db")
        spec = MagicMock()
        spec.name = "bash"
        spec.description = "Shell"
        spec.parameters = {"type": "object"}

        snapshot = FrozenToolRegistry.from_specs([spec])
        store.save(snapshot, "run-1")

        loaded = store.load(snapshot.snapshot_id)
        assert loaded is not None
        assert loaded.hash == snapshot.hash
        store.close()

    def test_load_for_run(self, tmp_path: Path) -> None:
        store = ToolSnapshotStore(tmp_path / "test.db")
        spec = MagicMock()
        spec.name = "bash"
        spec.description = "Shell"
        spec.parameters = {"type": "object"}

        snapshot = FrozenToolRegistry.from_specs([spec])
        store.save(snapshot, "run-1")

        loaded = store.load_for_run("run-1")
        assert loaded is not None
        assert loaded.hash == snapshot.hash
        store.close()

    def test_load_nonexistent_returns_none(self, tmp_path: Path) -> None:
        store = ToolSnapshotStore(tmp_path / "test.db")
        assert store.load("nonexistent") is None
        store.close()
