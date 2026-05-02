"""``FrozenToolRegistry.from_specs`` typed-spec contract.

After the strict-typing pass the classmethod takes ``list[ToolSpec]``
instead of ``list[Any]`` — verifies the canonical happy paths still
work and the allow/deny filters narrow correctly.
"""

from __future__ import annotations

from obscura.core.supervisor.tool_snapshot import FrozenToolRegistry
from obscura.core.types import ToolSpec


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object"},
        handler=lambda: None,
    )


def test_from_specs_sorts_by_name() -> None:
    registry = FrozenToolRegistry.from_specs([_spec("z"), _spec("a"), _spec("m")])
    assert list(registry.tool_names) == ["a", "m", "z"]


def test_from_specs_applies_allowlist() -> None:
    registry = FrozenToolRegistry.from_specs(
        [_spec("a"), _spec("b"), _spec("c")],
        allowlist=["a", "c"],
    )
    assert list(registry.tool_names) == ["a", "c"]


def test_from_specs_applies_denylist() -> None:
    registry = FrozenToolRegistry.from_specs(
        [_spec("a"), _spec("b"), _spec("c")],
        denylist=["b"],
    )
    assert list(registry.tool_names) == ["a", "c"]


def test_from_specs_assigns_order_index() -> None:
    """Order indices reflect post-sort position so snapshots are stable."""
    registry = FrozenToolRegistry.from_specs([_spec("z"), _spec("a")])
    assert registry.tools[0].name == "a"
    assert registry.tools[0].order_index == 0
    assert registry.tools[1].name == "z"
    assert registry.tools[1].order_index == 1


def test_hash_is_deterministic() -> None:
    """Two registries with the same tools produce the same snapshot hash."""
    r1 = FrozenToolRegistry.from_specs([_spec("a"), _spec("b")])
    r2 = FrozenToolRegistry.from_specs([_spec("b"), _spec("a")])  # different input order
    assert r1.hash == r2.hash


def test_hash_changes_with_different_tools() -> None:
    r1 = FrozenToolRegistry.from_specs([_spec("a")])
    r2 = FrozenToolRegistry.from_specs([_spec("a"), _spec("b")])
    assert r1.hash != r2.hash


def test_get_returns_none_for_missing() -> None:
    registry = FrozenToolRegistry.from_specs([_spec("a")])
    assert registry.get("a") is not None
    assert registry.get("missing") is None


def test_contains_works() -> None:
    registry = FrozenToolRegistry.from_specs([_spec("a"), _spec("b")])
    assert registry.contains("a")
    assert not registry.contains("missing")
