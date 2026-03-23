"""Tests for the ToolBroker registration quality gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from obscura.plugins.broker import RegistrationResult, ToolBroker
from obscura.plugins.policy import PluginPolicyEngine


def _make_policy() -> PluginPolicyEngine:
    return PluginPolicyEngine()


@dataclass
class FakeToolSpec:
    """Minimal ToolSpec stand-in for registration tests."""

    name: str = "test_tool"
    description: str = "A test tool"
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: Any = None

    def __post_init__(self) -> None:
        if self.handler is None:
            self.handler = lambda: "ok"


class TestRegistrationGate:
    def test_valid_tool_registers(self) -> None:
        broker = ToolBroker(policy_engine=_make_policy())
        spec = FakeToolSpec()
        result = broker.register_tool_spec(spec)
        assert result.status == "registered"
        assert spec.name in broker.registered_tools

    def test_missing_handler_quarantines(self) -> None:
        broker = ToolBroker(policy_engine=_make_policy())
        spec = FakeToolSpec(handler="not_callable")
        result = broker.register_tool_spec(spec)
        assert result.status == "quarantined"
        assert spec.name not in broker.registered_tools
        assert spec.name in broker.quarantined_tools

    def test_invalid_name_quarantines(self) -> None:
        broker = ToolBroker(policy_engine=_make_policy())
        spec = FakeToolSpec(name="")
        result = broker.register_tool_spec(spec)
        assert result.status == "quarantined"

    def test_invalid_name_special_chars_quarantines(self) -> None:
        broker = ToolBroker(policy_engine=_make_policy())
        spec = FakeToolSpec(name="bad name with spaces")
        result = broker.register_tool_spec(spec)
        assert result.status == "quarantined"

    def test_non_dict_parameters_quarantines(self) -> None:
        broker = ToolBroker(policy_engine=_make_policy())
        spec = FakeToolSpec(parameters="not a dict")  # type: ignore[arg-type]
        result = broker.register_tool_spec(spec)
        assert result.status == "quarantined"

    def test_missing_description_warns_but_registers(self) -> None:
        broker = ToolBroker(policy_engine=_make_policy())
        spec = FakeToolSpec(description="")
        result = broker.register_tool_spec(spec)
        assert result.status == "registered"
        assert any(i.level == "warning" for i in result.issues)

    def test_valid_dotted_name(self) -> None:
        broker = ToolBroker(policy_engine=_make_policy())
        spec = FakeToolSpec(name="m365.teams.message.send")
        result = broker.register_tool_spec(spec)
        assert result.status == "registered"

    def test_quarantined_tools_list(self) -> None:
        broker = ToolBroker(policy_engine=_make_policy())
        broker.register_tool_spec(FakeToolSpec(name="good"))
        broker.register_tool_spec(FakeToolSpec(name="", handler="bad"))
        assert "good" in broker.registered_tools
        assert "good" not in broker.quarantined_tools
