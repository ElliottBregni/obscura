"""Tests for obscura.plugins.capabilities — CapabilityResolver grant/deny/resolve logic."""

from __future__ import annotations

import pytest

from obscura.plugins.capabilities import (
    CapabilityDenial,
    CapabilityGrant,
    CapabilityResolver,
)
from obscura.plugins.models import CapabilitySpec, ToolContribution
from obscura.plugins.registries.capability_index import CapabilityIndex
from obscura.plugins.registries.tool_index import ToolIndex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_cap(
    cap_id: str,
    *,
    tools: tuple[str, ...] = (),
    requires_approval: bool = False,
    default_grant: bool = True,
) -> CapabilitySpec:
    return CapabilitySpec(
        id=cap_id,
        version="1.0.0",
        description=f"Test capability {cap_id}",
        tools=tools,
        requires_approval=requires_approval,
        default_grant=default_grant,
    )


def _make_tool(name: str, capability: str = "") -> ToolContribution:
    return ToolContribution(name=name, description=f"Tool {name}", capability=capability)


@pytest.fixture()
def cap_index() -> CapabilityIndex:
    idx = CapabilityIndex()
    idx.register(_make_cap("repo.read", tools=("list_files", "read_file")), "plugin-a")
    idx.register(
        _make_cap("pr.comment", tools=("post_comment",), requires_approval=True),
        "plugin-a",
    )
    idx.register(
        _make_cap("shell.exec", tools=("run_shell",), default_grant=False),
        "plugin-b",
    )
    return idx


@pytest.fixture()
def tool_index() -> ToolIndex:
    idx = ToolIndex()
    idx.register(_make_tool("list_files", capability="repo.read"), "plugin-a")
    idx.register(_make_tool("read_file", capability="repo.read"), "plugin-a")
    idx.register(_make_tool("post_comment", capability="pr.comment"), "plugin-a")
    idx.register(_make_tool("run_shell", capability="shell.exec"), "plugin-b")
    idx.register(_make_tool("help", capability=""), "plugin-a")  # no capability
    return idx


@pytest.fixture()
def resolver(cap_index: CapabilityIndex, tool_index: ToolIndex) -> CapabilityResolver:
    return CapabilityResolver(cap_index, tool_index)


# ---------------------------------------------------------------------------
# 1. Dataclass creation & defaults
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_capability_grant_defaults(self) -> None:
        g = CapabilityGrant(capability_id="repo.read", grantee_type="agent", grantee_id="a1")
        assert g.capability_id == "repo.read"
        assert g.grantee_type == "agent"
        assert g.granted_by == "default"
        assert g.requires_approval is False
        assert g.granted_at  # non-empty ISO string

    def test_capability_denial_defaults(self) -> None:
        d = CapabilityDenial(capability_id="pr.comment", grantee_type="agent", grantee_id="a1")
        assert d.denied_by == "policy"
        assert d.reason == ""


# ---------------------------------------------------------------------------
# 2–3. grant / deny basics
# ---------------------------------------------------------------------------


class TestGrantDeny:
    def test_grant_appears_in_resolve(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        assert "repo.read" in resolver.resolve_for_agent("agent-1")

    def test_grant_returns_grant_object(self, resolver: CapabilityResolver) -> None:
        g = resolver.grant("agent-1", "repo.read", granted_by="policy")
        assert isinstance(g, CapabilityGrant)
        assert g.granted_by == "policy"

    def test_deny_removes_conflicting_grant(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.deny("agent-1", "repo.read")
        assert "repo.read" not in resolver.resolve_for_agent("agent-1")

    def test_deny_returns_denial_object(self, resolver: CapabilityResolver) -> None:
        d = resolver.deny("agent-1", "repo.read", reason="too risky")
        assert isinstance(d, CapabilityDenial)
        assert d.reason == "too risky"


# ---------------------------------------------------------------------------
# 4–5. Grant-then-deny / deny-then-grant ordering
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_grant_then_deny_removes_capability(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.deny("agent-1", "repo.read")
        assert not resolver.is_granted("agent-1", "repo.read")

    def test_deny_then_grant_restores_capability(self, resolver: CapabilityResolver) -> None:
        resolver.deny("agent-1", "repo.read")
        resolver.grant("agent-1", "repo.read")
        assert resolver.is_granted("agent-1", "repo.read")


# ---------------------------------------------------------------------------
# 6. grant_defaults
# ---------------------------------------------------------------------------


class TestGrantDefaults:
    def test_grants_only_default_grant_caps(self, resolver: CapabilityResolver) -> None:
        grants = resolver.grant_defaults("agent-1")
        granted_ids = {g.capability_id for g in grants}
        # repo.read and pr.comment have default_grant=True; shell.exec does not
        assert "repo.read" in granted_ids
        assert "pr.comment" in granted_ids
        assert "shell.exec" not in granted_ids

    def test_grant_defaults_respects_existing_denial(self, resolver: CapabilityResolver) -> None:
        resolver.deny("agent-1", "repo.read")
        grants = resolver.grant_defaults("agent-1")
        granted_ids = {g.capability_id for g in grants}
        assert "repo.read" not in granted_ids
        # pr.comment should still be granted
        assert "pr.comment" in granted_ids

    def test_grant_defaults_sets_granted_by(self, resolver: CapabilityResolver) -> None:
        grants = resolver.grant_defaults("agent-1")
        assert all(g.granted_by == "plugin_default" for g in grants)


# ---------------------------------------------------------------------------
# 7. resolve_for_agent
# ---------------------------------------------------------------------------


class TestResolveForAgent:
    def test_returns_granted_minus_denied(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.grant("agent-1", "pr.comment")
        resolver.deny("agent-1", "pr.comment")
        result = resolver.resolve_for_agent("agent-1")
        assert result == {"repo.read"}


# ---------------------------------------------------------------------------
# 8–9. resolve_tools / resolve_tool_names
# ---------------------------------------------------------------------------


class TestResolveTools:
    def test_resolve_tools_includes_granted(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        tools = resolver.resolve_tools("agent-1")
        names = {t.name for t in tools}
        assert "list_files" in names
        assert "read_file" in names

    def test_resolve_tools_excludes_denied(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.deny("agent-1", "pr.comment")
        tools = resolver.resolve_tools("agent-1")
        names = {t.name for t in tools}
        assert "post_comment" not in names

    def test_resolve_tools_includes_no_capability_tools(self, resolver: CapabilityResolver) -> None:
        """Tools with no capability (empty string) are always visible."""
        tools = resolver.resolve_tools("agent-1")
        names = {t.name for t in tools}
        assert "help" in names

    def test_resolve_tool_names_returns_set_of_strings(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        names = resolver.resolve_tool_names("agent-1")
        assert isinstance(names, set)
        assert "list_files" in names
        assert "read_file" in names


# ---------------------------------------------------------------------------
# 10. is_granted
# ---------------------------------------------------------------------------


class TestIsGranted:
    def test_true_when_granted(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        assert resolver.is_granted("agent-1", "repo.read") is True

    def test_false_when_not_granted(self, resolver: CapabilityResolver) -> None:
        assert resolver.is_granted("agent-1", "repo.read") is False

    def test_false_when_denied(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.deny("agent-1", "repo.read")
        assert resolver.is_granted("agent-1", "repo.read") is False


# ---------------------------------------------------------------------------
# 11. requires_approval
# ---------------------------------------------------------------------------


class TestRequiresApproval:
    def test_true_for_approval_cap(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "pr.comment")
        assert resolver.requires_approval("agent-1", "pr.comment") is True

    def test_false_for_non_approval_cap(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        assert resolver.requires_approval("agent-1", "repo.read") is False

    def test_false_when_not_granted(self, resolver: CapabilityResolver) -> None:
        assert resolver.requires_approval("agent-1", "repo.read") is False


# ---------------------------------------------------------------------------
# 12. Multiple agents isolation
# ---------------------------------------------------------------------------


class TestMultipleAgents:
    def test_grants_are_isolated(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.grant("agent-2", "pr.comment")
        assert resolver.resolve_for_agent("agent-1") == {"repo.read"}
        assert resolver.resolve_for_agent("agent-2") == {"pr.comment"}

    def test_deny_on_one_agent_doesnt_affect_other(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.grant("agent-2", "repo.read")
        resolver.deny("agent-1", "repo.read")
        assert not resolver.is_granted("agent-1", "repo.read")
        assert resolver.is_granted("agent-2", "repo.read")


# ---------------------------------------------------------------------------
# 13. Empty state
# ---------------------------------------------------------------------------


class TestEmptyState:
    def test_no_grants_empty_resolve(self, resolver: CapabilityResolver) -> None:
        assert resolver.resolve_for_agent("agent-1") == set()

    def test_no_grants_resolve_tools_only_uncapped(self, resolver: CapabilityResolver) -> None:
        tools = resolver.resolve_tools("agent-1")
        names = {t.name for t in tools}
        # only the "help" tool with no capability should appear
        assert names == {"help"}

    def test_no_grants_empty_tool_names_plus_uncapped(self, resolver: CapabilityResolver) -> None:
        assert resolver.resolve_tool_names("agent-1") == {"help"}


# ---------------------------------------------------------------------------
# 14. Query methods: list_grants / list_denials / list_all_grantees
# ---------------------------------------------------------------------------


class TestQueryMethods:
    def test_list_grants(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.grant("agent-1", "pr.comment")
        grants = resolver.list_grants("agent-1")
        assert len(grants) == 2
        assert {g.capability_id for g in grants} == {"repo.read", "pr.comment"}

    def test_list_grants_empty(self, resolver: CapabilityResolver) -> None:
        assert resolver.list_grants("ghost") == []

    def test_list_denials(self, resolver: CapabilityResolver) -> None:
        resolver.deny("agent-1", "shell.exec", reason="unsafe")
        denials = resolver.list_denials("agent-1")
        assert len(denials) == 1
        assert denials[0].capability_id == "shell.exec"
        assert denials[0].reason == "unsafe"

    def test_list_denials_empty(self, resolver: CapabilityResolver) -> None:
        assert resolver.list_denials("ghost") == []

    def test_list_all_grantees(self, resolver: CapabilityResolver) -> None:
        resolver.grant("agent-1", "repo.read")
        resolver.deny("agent-2", "shell.exec")
        grantees = resolver.list_all_grantees()
        assert set(grantees) == {"agent-1", "agent-2"}

    def test_list_all_grantees_empty(self, resolver: CapabilityResolver) -> None:
        assert resolver.list_all_grantees() == []

    def test_list_grants_returns_copy(self, resolver: CapabilityResolver) -> None:
        """Mutating the returned list should not affect internal state."""
        resolver.grant("agent-1", "repo.read")
        grants = resolver.list_grants("agent-1")
        grants.clear()
        assert len(resolver.list_grants("agent-1")) == 1
