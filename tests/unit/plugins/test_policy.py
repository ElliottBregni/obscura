"""Comprehensive tests for obscura.plugins.policy module."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from obscura.plugins.policy import (
    PluginPolicyEngine,
    PolicyAction,
    PolicyDecision,
    PolicyRule,
    PolicyRuleSet,
    _glob_match,
)


# ---------------------------------------------------------------------------
# 1. PolicyAction enum
# ---------------------------------------------------------------------------


class TestPolicyAction:
    def test_values(self):
        assert PolicyAction.ALLOW.value == "allow"
        assert PolicyAction.DENY.value == "deny"
        assert PolicyAction.APPROVE.value == "approve"

    def test_is_str_subclass(self):
        assert isinstance(PolicyAction.ALLOW, str)


# ---------------------------------------------------------------------------
# 2. PolicyDecision
# ---------------------------------------------------------------------------


class TestPolicyDecision:
    def test_allow_is_allowed(self):
        d = PolicyDecision(action=PolicyAction.ALLOW)
        assert d.allowed is True
        assert d.requires_approval is False

    def test_deny_is_not_allowed(self):
        d = PolicyDecision(action=PolicyAction.DENY)
        assert d.allowed is False
        assert d.requires_approval is False

    def test_approve_is_allowed_and_requires_approval(self):
        d = PolicyDecision(action=PolicyAction.APPROVE)
        assert d.allowed is True
        assert d.requires_approval is True

    def test_reason_and_matched_rule(self):
        d = PolicyDecision(action=PolicyAction.ALLOW, reason="ok", matched_rule="r1")
        assert d.reason == "ok"
        assert d.matched_rule == "r1"

    def test_frozen(self):
        d = PolicyDecision(action=PolicyAction.ALLOW)
        with pytest.raises(AttributeError):
            d.action = PolicyAction.DENY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. PolicyRule defaults and fields
# ---------------------------------------------------------------------------


class TestPolicyRule:
    def test_defaults(self):
        r = PolicyRule()
        assert r.id == ""
        assert r.plugin is None
        assert r.trust_level is None
        assert r.capability is None
        assert r.tool is None
        assert r.agent is None
        assert r.environment is None
        assert r.action == PolicyAction.DENY
        assert r.reason == ""
        assert r.priority == 0

    def test_all_matchers(self):
        r = PolicyRule(
            id="test",
            plugin="myplugin",
            trust_level="verified",
            capability="read",
            tool="git_*",
            agent="reviewer",
            environment="prod",
            action=PolicyAction.ALLOW,
            reason="custom",
            priority=10,
        )
        assert r.id == "test"
        assert r.plugin == "myplugin"
        assert r.trust_level == "verified"
        assert r.capability == "read"
        assert r.tool == "git_*"
        assert r.agent == "reviewer"
        assert r.environment == "prod"
        assert r.action == PolicyAction.ALLOW
        assert r.reason == "custom"
        assert r.priority == 10


# ---------------------------------------------------------------------------
# 4. Default rules
# ---------------------------------------------------------------------------


class TestDefaultRules:
    def test_builtin_allowed(self):
        engine = PluginPolicyEngine()
        d = engine.can_load_plugin("anything", trust_level="builtin")
        assert d.allowed is True
        assert d.matched_rule == "default-allow-builtin"

    def test_verified_allowed(self):
        engine = PluginPolicyEngine()
        d = engine.can_load_plugin("anything", trust_level="verified")
        assert d.allowed is True
        assert d.matched_rule == "default-allow-verified"

    def test_community_allowed(self):
        engine = PluginPolicyEngine()
        d = engine.can_load_plugin("anything", trust_level="community")
        assert d.allowed is True
        assert d.matched_rule == "default-allow-community"

    def test_untrusted_denied(self):
        engine = PluginPolicyEngine()
        d = engine.can_load_plugin("anything", trust_level="untrusted")
        assert d.allowed is False
        assert d.matched_rule == "default-deny-untrusted"


# ---------------------------------------------------------------------------
# 5. can_load_plugin
# ---------------------------------------------------------------------------


class TestCanLoadPlugin:
    def test_builtin_trusted(self):
        engine = PluginPolicyEngine()
        assert engine.can_load_plugin("core-tools", trust_level="builtin").allowed

    def test_untrusted_denied(self):
        engine = PluginPolicyEngine()
        assert not engine.can_load_plugin("shady-pkg", trust_level="untrusted").allowed

    def test_community_allowed_default(self):
        engine = PluginPolicyEngine()
        assert engine.can_load_plugin("community-pkg", trust_level="community").allowed


# ---------------------------------------------------------------------------
# 6. can_execute_tool
# ---------------------------------------------------------------------------


class TestCanExecuteTool:
    def test_no_matching_rule_default_allow(self):
        engine = PluginPolicyEngine()
        d = engine.can_execute_tool("some_tool")
        assert d.allowed is True
        assert d.matched_rule == ""

    def test_matching_deny_rule(self):
        deny_rule = PolicyRule(
            id="deny-shell",
            tool="shell_exec",
            action=PolicyAction.DENY,
            reason="no shells",
            priority=10,
        )
        engine = PluginPolicyEngine(PolicyRuleSet([deny_rule]))
        d = engine.can_execute_tool("shell_exec")
        assert d.allowed is False
        assert d.matched_rule == "deny-shell"


# ---------------------------------------------------------------------------
# 7. can_grant_capability with agent filtering
# ---------------------------------------------------------------------------


class TestCanGrantCapability:
    def test_agent_matching(self):
        rule = PolicyRule(
            id="deny-reviewer-write",
            capability="write",
            agent="reviewer",
            action=PolicyAction.DENY,
            reason="reviewers cannot write",
            priority=10,
        )
        engine = PluginPolicyEngine(PolicyRuleSet([rule]))
        d = engine.can_grant_capability("write", agent_id="reviewer")
        assert d.allowed is False

    def test_agent_not_matching(self):
        rule = PolicyRule(
            id="deny-reviewer-write",
            capability="write",
            agent="reviewer",
            action=PolicyAction.DENY,
            reason="reviewers cannot write",
            priority=10,
        )
        engine = PluginPolicyEngine(PolicyRuleSet([rule]))
        d = engine.can_grant_capability("write", agent_id="admin")
        assert d.allowed is True


# ---------------------------------------------------------------------------
# 8. _glob_match
# ---------------------------------------------------------------------------


class TestGlobMatch:
    def test_none_pattern_matches_anything(self):
        assert _glob_match(None, "anything") is True

    def test_exact_match(self):
        assert _glob_match("foo", "foo") is True
        assert _glob_match("foo", "bar") is False

    def test_full_wildcard(self):
        assert _glob_match("*", "anything") is True

    def test_prefix_wildcard(self):
        assert _glob_match("shell_*", "shell_exec") is True
        assert _glob_match("shell_*", "git_clone") is False

    def test_suffix_wildcard(self):
        assert _glob_match("*_exec", "shell_exec") is True
        assert _glob_match("*_exec", "shell_run") is False

    def test_middle_wildcard(self):
        assert _glob_match("a*z", "abcz") is True
        assert _glob_match("a*z", "abcx") is False


# ---------------------------------------------------------------------------
# 9. Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    def test_higher_priority_wins(self):
        deny_rule = PolicyRule(
            id="deny-all-tools",
            tool="*",
            action=PolicyAction.DENY,
            reason="deny everything",
            priority=0,
        )
        allow_rule = PolicyRule(
            id="allow-shell",
            tool="shell_exec",
            action=PolicyAction.ALLOW,
            reason="shell is ok",
            priority=10,
        )
        engine = PluginPolicyEngine(PolicyRuleSet([deny_rule, allow_rule]))
        d = engine.can_execute_tool("shell_exec")
        assert d.allowed is True
        assert d.matched_rule == "allow-shell"

    def test_lower_priority_loses(self):
        deny_rule = PolicyRule(
            id="deny-all-tools",
            tool="*",
            action=PolicyAction.DENY,
            reason="deny everything",
            priority=10,
        )
        allow_rule = PolicyRule(
            id="allow-shell",
            tool="shell_exec",
            action=PolicyAction.ALLOW,
            reason="shell is ok",
            priority=0,
        )
        engine = PluginPolicyEngine(PolicyRuleSet([deny_rule, allow_rule]))
        d = engine.can_execute_tool("shell_exec")
        assert d.allowed is False
        assert d.matched_rule == "deny-all-tools"


# ---------------------------------------------------------------------------
# 10. Environment filtering
# ---------------------------------------------------------------------------


class TestEnvironmentFiltering:
    def test_rule_skipped_when_env_mismatch(self, monkeypatch):
        monkeypatch.setenv("OBSCURA_ENV", "dev")
        rule = PolicyRule(
            id="prod-deny",
            tool="deploy",
            environment="prod",
            action=PolicyAction.DENY,
            reason="no deploy in prod",
            priority=10,
        )
        engine = PluginPolicyEngine(PolicyRuleSet([rule]))
        d = engine.can_execute_tool("deploy")
        assert d.allowed is True  # rule skipped because env is dev

    def test_rule_applied_when_env_matches(self, monkeypatch):
        monkeypatch.setenv("OBSCURA_ENV", "prod")
        rule = PolicyRule(
            id="prod-deny",
            tool="deploy",
            environment="prod",
            action=PolicyAction.DENY,
            reason="no deploy in prod",
            priority=10,
        )
        engine = PluginPolicyEngine(PolicyRuleSet([rule]))
        d = engine.can_execute_tool("deploy")
        assert d.allowed is False


# ---------------------------------------------------------------------------
# 11. Custom rules via add_rule
# ---------------------------------------------------------------------------


class TestAddRule:
    def test_add_and_evaluate(self):
        engine = PluginPolicyEngine()
        engine.add_rule(PolicyRule(
            id="custom-deny",
            tool="dangerous_tool",
            action=PolicyAction.DENY,
            reason="too dangerous",
            priority=10,
        ))
        d = engine.can_execute_tool("dangerous_tool")
        assert d.allowed is False
        assert d.matched_rule == "custom-deny"


# ---------------------------------------------------------------------------
# 12. requires_approval convenience method
# ---------------------------------------------------------------------------


class TestRequiresApproval:
    def test_approve_action(self):
        rule = PolicyRule(
            id="approve-deploy",
            tool="deploy",
            action=PolicyAction.APPROVE,
            reason="needs sign-off",
            priority=10,
        )
        engine = PluginPolicyEngine(PolicyRuleSet([rule]))
        assert engine.requires_approval("deploy") is True

    def test_allow_action(self):
        engine = PluginPolicyEngine()
        assert engine.requires_approval("some_safe_tool") is False


# ---------------------------------------------------------------------------
# 13. YAML policy file loading
# ---------------------------------------------------------------------------


class TestYAMLLoading:
    def test_load_from_yaml(self, tmp_path):
        yaml_content = """\
rules:
  - id: yaml-deny-exec
    tool: shell_exec
    action: deny
    reason: no shell from yaml
    priority: 50
  - id: yaml-allow-git
    tool: git_*
    action: allow
    reason: git tools ok
    priority: 40
"""
        (tmp_path / "test.yaml").write_text(yaml_content)
        engine = PluginPolicyEngine.load(policies_dir=tmp_path)

        d = engine.can_execute_tool("shell_exec")
        assert d.allowed is False
        assert d.matched_rule == "yaml-deny-exec"

        d = engine.can_execute_tool("git_clone")
        assert d.allowed is True
        assert d.matched_rule == "yaml-allow-git"

    def test_load_empty_dir(self, tmp_path):
        engine = PluginPolicyEngine.load(policies_dir=tmp_path)
        # Should still have default rules
        assert len(engine.list_rules()) == len(PluginPolicyEngine._DEFAULT_RULES)

    def test_load_nonexistent_dir(self, tmp_path):
        engine = PluginPolicyEngine.load(policies_dir=tmp_path / "nope")
        assert len(engine.list_rules()) == len(PluginPolicyEngine._DEFAULT_RULES)

    def test_load_malformed_yaml(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("not: [valid: yaml: {{{")
        engine = PluginPolicyEngine.load(policies_dir=tmp_path)
        # Should gracefully skip bad file and still have defaults
        assert len(engine.list_rules()) >= len(PluginPolicyEngine._DEFAULT_RULES)


# ---------------------------------------------------------------------------
# 14. list_rules sorted by priority
# ---------------------------------------------------------------------------


class TestListRules:
    def test_sorted_by_priority_desc(self):
        rules = [
            PolicyRule(id="low", priority=1),
            PolicyRule(id="high", priority=100),
            PolicyRule(id="mid", priority=50),
        ]
        engine = PluginPolicyEngine(PolicyRuleSet(rules))
        listed = engine.list_rules()
        priorities = [r.priority for r in listed]
        assert priorities == sorted(priorities, reverse=True)

    def test_includes_defaults(self):
        engine = PluginPolicyEngine()
        ids = {r.id for r in engine.list_rules()}
        assert "default-allow-builtin" in ids
        assert "default-deny-untrusted" in ids


# ---------------------------------------------------------------------------
# PolicyRuleSet
# ---------------------------------------------------------------------------


class TestPolicyRuleSet:
    def test_sorted_rules(self):
        rs = PolicyRuleSet([
            PolicyRule(id="a", priority=5),
            PolicyRule(id="b", priority=10),
        ])
        assert rs.sorted_rules()[0].id == "b"
