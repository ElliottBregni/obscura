"""Plugin policy engine for Obscura.

Evaluates allow/deny/approve rules at plugin, capability, and tool levels.
Extends (wraps) the existing ``ToolPolicy`` in ``core/tool_policy.py``
rather than replacing it.

Rules are loaded from ``~/.obscura/policies/`` (YAML files) and can be
environment-aware.  The engine exposes a simple ask-style API::

    from obscura.plugins.policy import PluginPolicyEngine

    engine = PluginPolicyEngine.load()
    decision = engine.can_load_plugin("coingecko", trust_level="community")
    decision = engine.can_execute_tool("github_comment_pr", agent_id="reviewer")
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision model
# ---------------------------------------------------------------------------


class PolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVE = "approve"  # allowed but requires user confirmation


@dataclass(frozen=True)
class PolicyDecision:
    """Result of a policy evaluation."""
    action: PolicyAction
    reason: str = ""
    matched_rule: str = ""  # rule identifier for audit trail

    @property
    def allowed(self) -> bool:
        return self.action in (PolicyAction.ALLOW, PolicyAction.APPROVE)

    @property
    def requires_approval(self) -> bool:
        return self.action == PolicyAction.APPROVE


# ---------------------------------------------------------------------------
# Rule model
# ---------------------------------------------------------------------------


@dataclass
class PolicyRule:
    """A single policy rule loaded from YAML."""
    id: str = ""
    # Matchers (all are optional; a rule matches if ALL specified matchers match)
    plugin: str | None = None           # glob pattern, e.g. "obscura-*"
    trust_level: str | None = None      # "builtin" | "verified" | "community" | "untrusted"
    capability: str | None = None       # capability ID or glob
    tool: str | None = None             # tool name or glob
    agent: str | None = None            # agent ID or glob
    environment: str | None = None      # "dev" | "staging" | "prod" or None=any
    # Decision
    action: PolicyAction = PolicyAction.DENY
    reason: str = ""
    priority: int = 0                   # higher = evaluated first


@dataclass
class PolicyRuleSet:
    """An ordered collection of rules."""
    rules: list[PolicyRule] = field(default_factory=list)

    def sorted_rules(self) -> list[PolicyRule]:
        return sorted(self.rules, key=lambda r: r.priority, reverse=True)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _glob_match(pattern: str | None, value: str) -> bool:
    """Simple glob matching supporting only '*' as wildcard."""
    if pattern is None:
        return True  # no constraint
    if pattern == "*":
        return True
    if "*" in pattern:
        prefix, _, suffix = pattern.partition("*")
        return value.startswith(prefix) and value.endswith(suffix)
    return pattern == value


class PluginPolicyEngine:
    """Evaluates plugin/capability/tool policy rules.

    Load order (highest priority wins):
    1. Explicit rules from ``~/.obscura/policies/*.yaml``
    2. Default built-in rules (allow builtins, deny untrusted)
    """

    _DEFAULT_RULES: list[PolicyRule] = [
        PolicyRule(id="default-allow-builtin", trust_level="builtin",
                   action=PolicyAction.ALLOW, reason="Built-in plugins are trusted",
                   priority=-100),
        PolicyRule(id="default-allow-verified", trust_level="verified",
                   action=PolicyAction.ALLOW, reason="Verified plugins are trusted",
                   priority=-100),
        PolicyRule(id="default-allow-community", trust_level="community",
                   action=PolicyAction.ALLOW, reason="Community plugins allowed by default",
                   priority=-200),
        PolicyRule(id="default-deny-untrusted", trust_level="untrusted",
                   action=PolicyAction.DENY, reason="Untrusted plugins denied by default",
                   priority=-300),
    ]

    def __init__(self, ruleset: PolicyRuleSet | None = None) -> None:
        base = PolicyRuleSet(list(self._DEFAULT_RULES))
        if ruleset:
            base.rules.extend(ruleset.rules)
        self._ruleset = base
        self._env = os.environ.get("OBSCURA_ENV", "dev")

    # -- Factory -----------------------------------------------------------

    @classmethod
    def load(cls, policies_dir: Path | None = None) -> PluginPolicyEngine:
        """Load rules from ``~/.obscura/policies/``."""
        if policies_dir is None:
            policies_dir = Path.home() / ".obscura" / "policies"

        ruleset = PolicyRuleSet()
        if policies_dir.is_dir():
            try:
                import yaml  # type: ignore[import-untyped]
            except ImportError:
                logger.warning("PyYAML not installed — skipping policy files")
                return cls(ruleset)

            for f in sorted(policies_dir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(f.read_text()) or {}
                    for i, rd in enumerate(data.get("rules", [])):
                        rule = PolicyRule(
                            id=rd.get("id", f"{f.stem}-{i}"),
                            plugin=rd.get("plugin"),
                            trust_level=rd.get("trust_level"),
                            capability=rd.get("capability"),
                            tool=rd.get("tool"),
                            agent=rd.get("agent"),
                            environment=rd.get("environment"),
                            action=PolicyAction(rd.get("action", "deny")),
                            reason=rd.get("reason", ""),
                            priority=rd.get("priority", 0),
                        )
                        ruleset.rules.append(rule)
                    logger.debug("Loaded %d rules from %s", len(data.get("rules", [])), f)
                except Exception as exc:
                    logger.warning("Failed to parse policy file %s: %s", f, exc)

        return cls(ruleset)

    # -- Evaluation --------------------------------------------------------

    def _find_matching_rule(
        self,
        *,
        plugin: str | None = None,
        trust_level: str | None = None,
        capability: str | None = None,
        tool: str | None = None,
        agent: str | None = None,
    ) -> PolicyRule | None:
        for rule in self._ruleset.sorted_rules():
            # Environment filter
            if rule.environment and rule.environment != self._env:
                continue
            # Match all specified criteria
            if rule.plugin is not None and (plugin is None or not _glob_match(rule.plugin, plugin)):
                continue
            if rule.trust_level is not None and (trust_level is None or rule.trust_level != trust_level):
                continue
            if rule.capability is not None and (capability is None or not _glob_match(rule.capability, capability)):
                continue
            if rule.tool is not None and (tool is None or not _glob_match(rule.tool, tool)):
                continue
            if rule.agent is not None and (agent is None or not _glob_match(rule.agent, agent)):
                continue
            return rule
        return None

    def _decide(self, **kwargs: Any) -> PolicyDecision:
        rule = self._find_matching_rule(**kwargs)
        if rule is None:
            return PolicyDecision(PolicyAction.ALLOW, reason="No matching rule — default allow")
        return PolicyDecision(
            action=rule.action,
            reason=rule.reason,
            matched_rule=rule.id,
        )

    def can_load_plugin(self, plugin_id: str, trust_level: str = "community") -> PolicyDecision:
        return self._decide(plugin=plugin_id, trust_level=trust_level)

    def can_grant_capability(self, capability_id: str, agent_id: str) -> PolicyDecision:
        return self._decide(capability=capability_id, agent=agent_id)

    def can_execute_tool(self, tool_name: str, agent_id: str | None = None) -> PolicyDecision:
        return self._decide(tool=tool_name, agent=agent_id)

    def requires_approval(self, tool_name: str, agent_id: str | None = None) -> bool:
        decision = self.can_execute_tool(tool_name, agent_id)
        return decision.requires_approval

    # -- Inspection --------------------------------------------------------

    def list_rules(self) -> list[PolicyRule]:
        return self._ruleset.sorted_rules()

    def add_rule(self, rule: PolicyRule) -> None:
        self._ruleset.rules.append(rule)


__all__ = [
    "PolicyAction",
    "PolicyDecision",
    "PolicyRule",
    "PolicyRuleSet",
    "PluginPolicyEngine",
]
