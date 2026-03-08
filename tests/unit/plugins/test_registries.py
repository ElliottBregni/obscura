"""Comprehensive tests for all five runtime registries in the Obscura plugin platform."""

from __future__ import annotations

import pytest

from obscura.plugins.models import (
    CapabilitySpec,
    InstructionSpec,
    PluginSpec,
    PluginStatus,
    ToolContribution,
    WorkflowSpec,
)
from obscura.plugins.registries.capability_index import CapabilityIndex
from obscura.plugins.registries.instruction_index import InstructionIndex
from obscura.plugins.registries.plugin_index import PluginIndex
from obscura.plugins.registries.tool_index import ToolIndex
from obscura.plugins.registries.workflow_index import WorkflowIndex


# ---------------------------------------------------------------------------
# Helpers — reusable model factories
# ---------------------------------------------------------------------------

def _plugin_spec(
    pid: str = "acme-scanner",
    name: str = "Acme Scanner",
    version: str = "1.0.0",
    trust_level: str = "community",
    runtime_type: str = "native",
    source_type: str = "local",
    **kwargs,
) -> PluginSpec:
    return PluginSpec(
        id=pid,
        name=name,
        version=version,
        trust_level=trust_level,
        runtime_type=runtime_type,
        source_type=source_type,
        **kwargs,
    )


def _plugin_status(
    pid: str = "acme-scanner",
    enabled: bool = True,
    state: str = "enabled",
) -> PluginStatus:
    return PluginStatus(plugin_id=pid, enabled=enabled, state=state)


def _cap_spec(
    cid: str = "repo.read",
    version: str = "1.0.0",
    description: str = "Read repository files",
    tools: tuple[str, ...] = ("read_file", "list_dir"),
    requires_approval: bool = False,
    default_grant: bool = True,
) -> CapabilitySpec:
    return CapabilitySpec(
        id=cid,
        version=version,
        description=description,
        tools=tools,
        requires_approval=requires_approval,
        default_grant=default_grant,
    )


def _tool_contrib(
    name: str = "read_file",
    description: str = "Read a file",
    capability: str = "repo.read",
    side_effects: str = "read",
) -> ToolContribution:
    return ToolContribution(
        name=name,
        description=description,
        capability=capability,
        side_effects=side_effects,
    )


def _workflow_spec(
    wid: str = "wf.scan",
    version: str = "1.0.0",
    name: str = "Scan Workflow",
    description: str = "Scans repo",
    required_capabilities: tuple[str, ...] = ("repo.read",),
) -> WorkflowSpec:
    return WorkflowSpec(
        id=wid,
        version=version,
        name=name,
        description=description,
        required_capabilities=required_capabilities,
    )


def _instr_spec(
    iid: str = "instr.safety",
    version: str = "1.0.0",
    scope: str = "global",
    content: str = "Always be safe.",
    priority: int = 50,
) -> InstructionSpec:
    return InstructionSpec(
        id=iid, version=version, scope=scope, content=content, priority=priority,
    )


# ===================================================================
# PluginIndex
# ===================================================================

class TestPluginIndex:
    """Tests for PluginIndex."""

    def test_register_and_get(self):
        idx = PluginIndex()
        spec = _plugin_spec()
        idx.register(spec)
        assert idx.get("acme-scanner") is spec
        assert idx.get("nonexistent") is None

    def test_register_with_status(self):
        idx = PluginIndex()
        spec = _plugin_spec()
        status = _plugin_status()
        idx.register(spec, status)
        assert idx.get_status("acme-scanner") is status

    def test_list_all(self):
        idx = PluginIndex()
        s1 = _plugin_spec(pid="a-one", name="One")
        s2 = _plugin_spec(pid="b-two", name="Two")
        idx.register(s1)
        idx.register(s2)
        assert len(idx.list_all()) == 2

    def test_len_and_contains(self):
        idx = PluginIndex()
        assert len(idx) == 0
        idx.register(_plugin_spec())
        assert len(idx) == 1
        assert "acme-scanner" in idx
        assert "nope" not in idx

    def test_set_status(self):
        idx = PluginIndex()
        idx.register(_plugin_spec())
        new_status = _plugin_status(state="active")
        idx.set_status("acme-scanner", new_status)
        assert idx.get_status("acme-scanner").state == "active"

    def test_list_enabled(self):
        idx = PluginIndex()
        idx.register(_plugin_spec(pid="a"), _plugin_status(pid="a", enabled=True))
        idx.register(_plugin_spec(pid="b"), _plugin_status(pid="b", enabled=False))
        idx.register(_plugin_spec(pid="c"))  # no status → default enabled=False
        enabled = idx.list_enabled()
        assert len(enabled) == 1
        assert enabled[0].id == "a"

    def test_filter_by_trust(self):
        idx = PluginIndex()
        idx.register(_plugin_spec(pid="a", trust_level="builtin"))
        idx.register(_plugin_spec(pid="b", trust_level="community"))
        idx.register(_plugin_spec(pid="c", trust_level="builtin"))
        assert len(idx.filter_by_trust("builtin")) == 2
        assert len(idx.filter_by_trust("community")) == 1
        assert len(idx.filter_by_trust("builtin", "community")) == 3

    def test_filter_by_runtime(self):
        idx = PluginIndex()
        idx.register(_plugin_spec(pid="a", runtime_type="native"))
        idx.register(_plugin_spec(pid="b", runtime_type="mcp"))
        assert len(idx.filter_by_runtime("native")) == 1
        assert len(idx.filter_by_runtime("native", "mcp")) == 2

    def test_register_overwrites_same_id(self):
        idx = PluginIndex()
        idx.register(_plugin_spec(pid="x", name="V1"))
        idx.register(_plugin_spec(pid="x", name="V2"))
        assert len(idx) == 1
        assert idx.get("x").name == "V2"


# ===================================================================
# CapabilityIndex
# ===================================================================

class TestCapabilityIndex:
    """Tests for CapabilityIndex."""

    def test_register_and_get(self):
        idx = CapabilityIndex()
        cap = _cap_spec()
        idx.register(cap, "acme.scanner")
        assert idx.get("repo.read") is cap
        assert idx.get("missing") is None

    def test_len_and_contains(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(), "p1")
        assert len(idx) == 1
        assert "repo.read" in idx
        assert "missing" not in idx

    def test_list_all(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read"), "p1")
        idx.register(_cap_spec(cid="repo.write"), "p1")
        assert len(idx.list_all()) == 2

    def test_tools_for_capability(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read", tools=("read_file", "list_dir")), "p1")
        assert idx.tools_for_capability("repo.read") == ("read_file", "list_dir")
        assert idx.tools_for_capability("nonexistent") == ()

    def test_tools_for_capabilities_multiple(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read", tools=("read_file",)), "p1")
        idx.register(_cap_spec(cid="repo.write", tools=("write_file",)), "p1")
        result = idx.tools_for_capabilities({"repo.read", "repo.write"})
        assert result == {"read_file", "write_file"}

    def test_capabilities_with_default_grant(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read", default_grant=True), "p1")
        idx.register(_cap_spec(cid="repo.write", default_grant=False), "p1")
        granted = idx.capabilities_with_default_grant()
        assert len(granted) == 1
        assert granted[0].id == "repo.read"

    def test_capabilities_requiring_approval(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read", requires_approval=False), "p1")
        idx.register(_cap_spec(cid="repo.write", requires_approval=True), "p1")
        approval = idx.capabilities_requiring_approval()
        assert len(approval) == 1
        assert approval[0].id == "repo.write"

    def test_get_owner(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read"), "plugin.alpha")
        assert idx.get_owner("repo.read") == "plugin.alpha"
        assert idx.get_owner("missing") is None

    def test_filter_by_plugin(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read"), "p1")
        idx.register(_cap_spec(cid="repo.write"), "p2")
        idx.register(_cap_spec(cid="repo.exec"), "p1")
        result = idx.filter_by_plugin("p1")
        assert len(result) == 2
        ids = {c.id for c in result}
        assert ids == {"repo.read", "repo.exec"}

    def test_overwrite_warns_different_owner(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read"), "p1")
        idx.register(_cap_spec(cid="repo.read"), "p2")
        assert idx.get_owner("repo.read") == "p2"

    def test_re_register_same_owner_no_conflict(self):
        idx = CapabilityIndex()
        idx.register(_cap_spec(cid="repo.read"), "p1")
        idx.register(_cap_spec(cid="repo.read"), "p1")
        assert len(idx) == 1


# ===================================================================
# ToolIndex
# ===================================================================

class TestToolIndex:
    """Tests for ToolIndex."""

    def test_register_and_get(self):
        idx = ToolIndex()
        tool = _tool_contrib()
        idx.register(tool, "p1")
        assert idx.get("read_file") is tool
        assert idx.get("missing") is None

    def test_len_and_contains(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="a"), "p1")
        assert len(idx) == 1
        assert "a" in idx
        assert "z" not in idx

    def test_list_all_and_names(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="a"), "p1")
        idx.register(_tool_contrib(name="b"), "p1")
        assert len(idx.list_all()) == 2
        assert sorted(idx.names()) == ["a", "b"]

    def test_get_owner(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="t1"), "plugin.alpha")
        assert idx.get_owner("t1") == "plugin.alpha"
        assert idx.get_owner("missing") is None

    def test_filter_by_capability(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="rf", capability="repo.read"), "p1")
        idx.register(_tool_contrib(name="wf", capability="repo.write"), "p1")
        idx.register(_tool_contrib(name="ef", capability="repo.read"), "p1")
        result = idx.filter_by_capability("repo.read")
        assert len(result) == 2
        assert {t.name for t in result} == {"rf", "ef"}

    def test_filter_by_side_effects(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="a", side_effects="none"), "p1")
        idx.register(_tool_contrib(name="b", side_effects="read"), "p1")
        idx.register(_tool_contrib(name="c", side_effects="write"), "p1")
        assert len(idx.filter_by_side_effects("read")) == 1
        assert len(idx.filter_by_side_effects("none", "write")) == 2

    def test_visible_for_capabilities_granted(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="rf", capability="repo.read"), "p1")
        idx.register(_tool_contrib(name="wf", capability="repo.write"), "p1")
        idx.register(_tool_contrib(name="free", capability=""), "p1")
        visible = idx.visible_for_capabilities({"repo.read"})
        names = {t.name for t in visible}
        assert "rf" in names
        assert "free" in names
        assert "wf" not in names

    def test_visible_for_capabilities_empty_grants_only_uncapped(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="rf", capability="repo.read"), "p1")
        idx.register(_tool_contrib(name="free", capability=""), "p1")
        visible = idx.visible_for_capabilities(set())
        assert len(visible) == 1
        assert visible[0].name == "free"

    def test_filter_by_plugin(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="a"), "p1")
        idx.register(_tool_contrib(name="b"), "p2")
        assert len(idx.filter_by_plugin("p1")) == 1

    def test_overwrite_warns_different_owner(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="a"), "p1")
        idx.register(_tool_contrib(name="a"), "p2")
        assert idx.get_owner("a") == "p2"

    def test_re_register_same_owner(self):
        idx = ToolIndex()
        idx.register(_tool_contrib(name="a"), "p1")
        idx.register(_tool_contrib(name="a"), "p1")
        assert len(idx) == 1


# ===================================================================
# WorkflowIndex
# ===================================================================

class TestWorkflowIndex:
    """Tests for WorkflowIndex."""

    def test_register_and_get(self):
        idx = WorkflowIndex()
        wf = _workflow_spec()
        idx.register(wf, "p1")
        assert idx.get("wf.scan") is wf
        assert idx.get("missing") is None

    def test_list_all(self):
        idx = WorkflowIndex()
        idx.register(_workflow_spec(wid="wf.a"), "p1")
        idx.register(_workflow_spec(wid="wf.b"), "p1")
        assert len(idx.list_all()) == 2

    def test_len_and_contains(self):
        idx = WorkflowIndex()
        idx.register(_workflow_spec(), "p1")
        assert len(idx) == 1
        assert "wf.scan" in idx
        assert "nope" not in idx

    def test_get_owner(self):
        idx = WorkflowIndex()
        idx.register(_workflow_spec(wid="wf.a"), "plugin.alpha")
        assert idx.get_owner("wf.a") == "plugin.alpha"
        assert idx.get_owner("missing") is None

    def test_executable_with_all_caps_granted(self):
        idx = WorkflowIndex()
        idx.register(
            _workflow_spec(wid="wf.full", required_capabilities=("repo.read", "repo.write")),
            "p1",
        )
        idx.register(
            _workflow_spec(wid="wf.readonly", required_capabilities=("repo.read",)),
            "p1",
        )
        result = idx.executable_with({"repo.read"})
        assert len(result) == 1
        assert result[0].id == "wf.readonly"

    def test_executable_with_superset_grants(self):
        idx = WorkflowIndex()
        idx.register(
            _workflow_spec(wid="wf.a", required_capabilities=("repo.read",)),
            "p1",
        )
        result = idx.executable_with({"repo.read", "repo.write", "net.access"})
        assert len(result) == 1

    def test_executable_with_no_required_caps(self):
        idx = WorkflowIndex()
        idx.register(
            _workflow_spec(wid="wf.free", required_capabilities=()),
            "p1",
        )
        result = idx.executable_with(set())
        assert len(result) == 1

    def test_executable_with_empty_grants_filters_out_requiring(self):
        idx = WorkflowIndex()
        idx.register(
            _workflow_spec(wid="wf.need", required_capabilities=("repo.read",)),
            "p1",
        )
        assert idx.executable_with(set()) == []

    def test_filter_by_plugin(self):
        idx = WorkflowIndex()
        idx.register(_workflow_spec(wid="wf.a"), "p1")
        idx.register(_workflow_spec(wid="wf.b"), "p2")
        assert len(idx.filter_by_plugin("p1")) == 1


# ===================================================================
# InstructionIndex
# ===================================================================

class TestInstructionIndex:
    """Tests for InstructionIndex."""

    def test_register_and_get(self):
        idx = InstructionIndex()
        instr = _instr_spec()
        idx.register(instr, "p1")
        assert idx.get("instr.safety") is instr
        assert idx.get("missing") is None

    def test_list_all(self):
        idx = InstructionIndex()
        idx.register(_instr_spec(iid="i.a"), "p1")
        idx.register(_instr_spec(iid="i.b"), "p1")
        assert len(idx.list_all()) == 2

    def test_len(self):
        idx = InstructionIndex()
        assert len(idx) == 0
        idx.register(_instr_spec(), "p1")
        assert len(idx) == 1

    def test_for_scope(self):
        idx = InstructionIndex()
        idx.register(_instr_spec(iid="i.g1", scope="global"), "p1")
        idx.register(_instr_spec(iid="i.a1", scope="agent"), "p1")
        idx.register(_instr_spec(iid="i.g2", scope="global"), "p1")
        result = idx.for_scope("global")
        assert len(result) == 2
        assert all(i.scope == "global" for i in result)

    def test_for_scope_sorted_by_priority(self):
        idx = InstructionIndex()
        idx.register(_instr_spec(iid="i.low", scope="global", priority=100, content="low"), "p1")
        idx.register(_instr_spec(iid="i.high", scope="global", priority=10, content="high"), "p1")
        idx.register(_instr_spec(iid="i.mid", scope="global", priority=50, content="mid"), "p1")
        result = idx.for_scope("global")
        priorities = [i.priority for i in result]
        assert priorities == [10, 50, 100]

    def test_assemble_concatenates_in_priority_order(self):
        idx = InstructionIndex()
        idx.register(_instr_spec(iid="i.b", scope="global", priority=20, content="Second"), "p1")
        idx.register(_instr_spec(iid="i.a", scope="global", priority=10, content="First"), "p1")
        idx.register(_instr_spec(iid="i.c", scope="global", priority=30, content="Third"), "p1")
        assembled = idx.assemble("global")
        assert assembled == "First\n\nSecond\n\nThird"

    def test_assemble_empty_scope(self):
        idx = InstructionIndex()
        idx.register(_instr_spec(iid="i.a", scope="agent"), "p1")
        assert idx.assemble("global") == ""

    def test_assemble_default_scope_is_global(self):
        idx = InstructionIndex()
        idx.register(_instr_spec(iid="i.a", scope="global", content="Hello"), "p1")
        assert idx.assemble() == "Hello"

    def test_filter_by_plugin(self):
        idx = InstructionIndex()
        idx.register(_instr_spec(iid="i.a"), "p1")
        idx.register(_instr_spec(iid="i.b"), "p2")
        assert len(idx.filter_by_plugin("p1")) == 1

    def test_for_scope_session(self):
        idx = InstructionIndex()
        idx.register(_instr_spec(iid="i.s1", scope="session", content="session rule"), "p1")
        idx.register(_instr_spec(iid="i.g1", scope="global", content="global rule"), "p1")
        result = idx.for_scope("session")
        assert len(result) == 1
        assert result[0].id == "i.s1"
