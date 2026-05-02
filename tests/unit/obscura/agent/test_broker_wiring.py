"""Tests for ToolBroker registration and introspection."""

from __future__ import annotations

from obscura.plugins.broker import ToolBroker
from obscura.plugins.policy import PluginPolicyEngine


def _make_broker() -> ToolBroker:
    """Create a ToolBroker with a default policy engine."""
    engine = PluginPolicyEngine()
    return ToolBroker(policy_engine=engine)


def _dummy_handler(**kwargs: object) -> str:
    return "ok"


def test_broker_register_tool_stores_handler() -> None:
    broker = _make_broker()
    broker.register_tool("my_tool", _dummy_handler)
    assert "my_tool" in broker.registered_tools


def test_broker_register_tool_stores_schema() -> None:
    broker = _make_broker()
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    broker.register_tool("schema_tool", _dummy_handler, parameters=schema)
    assert "schema_tool" in broker.schemas
    assert broker.schemas["schema_tool"] == schema


def test_broker_register_tool_no_schema() -> None:
    broker = _make_broker()
    broker.register_tool("no_schema_tool", _dummy_handler)
    assert "no_schema_tool" not in broker.schemas


def test_broker_registered_tools_property() -> None:
    broker = _make_broker()
    broker.register_tool("alpha", _dummy_handler)
    broker.register_tool("beta", _dummy_handler)
    names = broker.registered_tools
    assert isinstance(names, list)
    assert "alpha" in names
    assert "beta" in names


def test_broker_schemas_property() -> None:
    broker = _make_broker()
    schema_a = {"type": "object"}
    broker.register_tool("a", _dummy_handler, parameters=schema_a)
    schemas = broker.schemas
    assert "a" in schemas
    # Verify it is a copy, not the internal dict
    schemas["injected"] = {}  # type: ignore[assignment]
    assert "injected" not in broker.schemas
